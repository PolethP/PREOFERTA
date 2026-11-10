import pyodbc
import json
import os
import sys
import traceback
from decimal import Decimal


def obtener_ruta_archivo(nombre_archivo):
    if getattr(sys, 'frozen', False):
        ruta_base = os.path.dirname(sys.executable)
    else:
        ruta_base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(ruta_base, nombre_archivo)

ruta_config = obtener_ruta_archivo("config.json")
with open(ruta_config, "r", encoding="utf-8") as f:
    cfg = json.load(f)["Configuracion"]

TABLA = f'"{cfg["schema"]}"."@PF_DET_PREOFERTA"'
TABLA_CAB = f'"{cfg["schema"]}"."@PF_CAB_PREOFERTA"' 

def limpiar_para_hana(valor):
    if valor is None:
        return ""
    valor = str(valor)
    valor = valor.encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")
    valor = "".join(c for c in valor if c.isprintable() or c in ("\n", "\r", "\t"))
    return valor.strip()

def texto(v):
    return limpiar_para_hana(v)

def numero(v):
    if v is None or v == "":
        return 0
    try:
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, (int, float)):
            return v
        v = str(v).strip().replace(",", ".")
        return float(v) if v else 0
    except Exception:
        return 0

# Se construyen las sentencias completas antes de ejecutar DELETE/INSERT.
# Los valores se pasan como parámetros: no se concatenan datos de usuario al SQL.
TAMANO_LOTE = 100


def preparar_inserts(tabla, columnas, filas, columnas_numericas, tamano_lote):
    if not filas:
        return []
    proyeccion = ", ".join(
        "CAST(? AS DECIMAL(19,6))" if i in columnas_numericas
        else "CAST(? AS NVARCHAR(5000))"
        for i in range(len(columnas))
    )
    select_fila = "SELECT " + proyeccion + " FROM DUMMY"
    encabezado = "INSERT INTO " + tabla + " (" + ", ".join('"' + c + '"' for c in columnas) + ")\n"
    sentencias = []
    for inicio in range(0, len(filas), tamano_lote):
        lote = filas[inicio:inicio + tamano_lote]
        parametros = []
        for fila in lote:
            if len(fila) != len(columnas):
                raise ValueError(f"Número de columnas incorrecto en {tabla}")
            for i, valor in enumerate(fila):
                if i in columnas_numericas:
                    cantidad = Decimal(str(valor))
                    if not cantidad.is_finite():
                        raise ValueError(f"Cantidad no finita en {tabla}")
                    parametros.append(cantidad)
                else:
                    valor = texto(valor)
                    if len(valor) > 5000:
                        raise ValueError(f"Texto mayor a 5000 caracteres: {tabla}.{columnas[i]}")
                    parametros.append(valor)
        sql = encabezado + "\nUNION ALL\n".join([select_fila] * len(lote))
        sentencias.append((sql, tuple(parametros), inicio + 1, len(lote)))
    return sentencias


def insertar_preoferta_sillas(datos, usuario=None, tamano_lote=TAMANO_LOTE):
    # Validar antes de abrir conexión o limpiar datos.
    datos = list(datos)
    for idx, d in enumerate(datos, start=1):
        usr = texto(usuario if usuario is not None else d.get("PF_USR"))
        if not usr:
            raise ValueError(f"Falta el código de usuario en la fila {idx}")
        d["PF_USR"] = usr
        componentes = texto(d.get("PF_COMCONS"))
        cantidades = texto(d.get("PF_CCON"))
        if componentes or cantidades:
            listas = [texto(d.get(campo)).split(" / ")
                      for campo in ("PF_SOL", "PF_LINEA", "PF_COMCONS", "PF_CCON")]
            if len({len(lista) for lista in listas}) != 1 or any(not x.strip() for lista in listas for x in lista):
                raise ValueError(f"Consumos sin correspondencia solicitud/línea/componente/cantidad: fila {idx}")
            for valor in listas[-1]:
                cantidad = Decimal(valor)
                if not cantidad.is_finite() or cantidad < 0:
                    raise ValueError(f"Cantidad consumida inválida: fila {idx}, {valor}")
    if not isinstance(tamano_lote, int) or tamano_lote <= 0:
        raise ValueError("El tamaño del lote debe ser un entero positivo")
    print("Inicio")
    cn = None
    cur = None
    try:
        cn = pyodbc.connect(
            f"DSN={cfg['dns']};UID={cfg['user']};PWD={cfg['password']}",
            autocommit=False
        )
        cn.setdecoding(pyodbc.SQL_CHAR, encoding="utf-8")
        cn.setdecoding(pyodbc.SQL_WCHAR, encoding="utf-8")
        cn.setencoding(encoding="utf-8")
        cur = cn.cursor()

        # Solo lectura: determinar qué códigos sobrevivirán a la limpieza final.
        cur.execute(f'''SELECT "Code", "Name", "U_PF_CODE" FROM {TABLA_CAB}
            WHERE IFNULL(TRIM("U_PF_OFE"), '') <> ''
        ''')
        conservados = [(texto(r[0]), texto(r[1]), texto(r[2])) for r in cur.fetchall()]
        usados_cab = {code for code, _, _ in conservados}
        nombres_cab = {nombre for _, nombre, _ in conservados}
        usados_det = {ref or nombre.split("_", 1)[0]
                      for _, nombre, ref in conservados if ref or nombre}
        contador_det = 0
        contador_cab = 0
        filas_det = []
        filas_cab = []

        # Acumular TODOS los datos. Este bucle no ejecuta INSERT ni DELETE.
        for d in datos:
            contador_det += 1
            while str(contador_det) in usados_det:
                contador_det += 1
            code = str(contador_det)
            usados_det.add(code)
            filas_det.append((
                code, code, texto(d.get("PF_SERIE")),
                texto(d.get("PF_COD_PROV")), texto(d.get("PF_PROVEEDOR")),
                texto(d.get("PF_SOL")), texto(d.get("PF_PADRE")),
                numero(d.get("PF_CANT")), numero(d.get("PF_COM")),
                numero(d.get("PF_INC")), numero(d.get("PF_TOT")),
                texto(d.get("PF_CFAL")), texto(d.get("PF_CAFAL")),
                texto(d.get("PF_TIPO")), texto(d.get("PF_LINEA")),
                texto(d.get("PF_COMP")), texto(d.get("PF_CCOMPRA")),
                texto(d.get("PF_PCOMPRA")), texto(d.get("PF_COMCONS")),
                texto(d.get("PF_CCON")), texto(d.get("PF_USR"))
            ))
            if texto(d.get("PF_COMCONS")):
                listas = [texto(d.get(campo)).split(" / ")
                          for campo in ("PF_SOL", "PF_LINEA", "PF_COMCONS", "PF_CCON")]
                for sol, linea, item, cantidad_texto in zip(*listas):
                    cantidad = Decimal(cantidad_texto)
                    if cantidad == 0:
                        continue
                    contador_cab += 1
                    while str(contador_cab) in usados_cab or str(contador_cab) in nombres_cab:
                        contador_cab += 1
                    code_cab = str(contador_cab)
                    usados_cab.add(code_cab)
                    nombres_cab.add(code_cab)
                    filas_cab.append((code_cab, code_cab, texto(sol), texto(linea),
                                      texto(item), cantidad, code))

        columnas_det = (
            "Code", "Name", "U_PF_SERIE", "U_PF_COD_PROV", "U_PF_PROVEEDOR",
            "U_PF_SOL", "U_PF_PADRE", "U_PF_CANT", "U_PF_COM", "U_PF_INC",
            "U_PF_TOT", "U_PF_CFAL", "U_PF_CAFAL", "U_PF_TIPO", "U_PF_LINEA",
            "U_PF_COMP", "U_PF_CCOMPRA", "U_PF_PCOMPRA",
            "U_PF_COMCONS", "U_PF_CCON", "U_PF_USR"
        )
        # Campos manuales OFE/PROC/LINOF/CANTOF no forman parte del INSERT.
        columnas_cab = ("Code", "Name", "U_PF_SOL", "U_PF_LIN", "U_PF_ITEM", "U_PF_CANT", "U_PF_CODE")
        inserts_det = preparar_inserts(TABLA, columnas_det, filas_det, {7, 8, 9, 10}, tamano_lote)
        inserts_cab = preparar_inserts(TABLA_CAB, columnas_cab, filas_cab, {5}, tamano_lote)
        print(f"Preparados: {len(filas_det)} detalles y {len(filas_cab)} desgloses")

        # FASE FINAL: limpiar e insertar por lotes en una única transacción.
        cur.execute(f"DELETE FROM {TABLA_CAB} WHERE IFNULL(TRIM(\"U_PF_OFE\"), '') = ''")
        cur.execute(f'''DELETE FROM {TABLA}
            WHERE "Code" NOT IN (
                SELECT CASE
                    WHEN IFNULL(TRIM("U_PF_CODE"), '') <> '' THEN TRIM("U_PF_CODE")
                    WHEN LOCATE("Name", '_') > 0
                    THEN SUBSTRING("Name", 1, LOCATE("Name", '_') - 1)
                    ELSE "Name"
                END FROM {TABLA_CAB}
                WHERE IFNULL(TRIM("U_PF_CODE"), '') <> '' OR "Name" IS NOT NULL
            )''')
        for tabla, sentencias in ((TABLA, inserts_det), (TABLA_CAB, inserts_cab)):
            for sql, parametros, inicio, cantidad in sentencias:
                try:
                    cur.execute(sql, parametros)
                except Exception as exc:
                    raise RuntimeError(
                        f"Error en lote de {tabla}: filas {inicio} a {inicio + cantidad - 1}"
                    ) from exc
        cn.commit()
        print(f"Insertados: {len(filas_det)} detalles en {len(inserts_det)} lotes; "
              f"{len(filas_cab)} desgloses en {len(inserts_cab)} lotes")
    except Exception:
        if cn is not None:
            cn.rollback()
        print("Error al guardar preoferta: transacción revertida")
        traceback.print_exc()
        raise
    finally:
        if cur is not None:
            cur.close()
        if cn is not None:
            cn.close()
