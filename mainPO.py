import pyodbc
import json
from decimal import Decimal
import tkinter as tk
from tkinter import ttk
import sys
import os
import argparse
from types import SimpleNamespace

# =====================================================
# UTIL
# =====================================================
def D(v):
    try:
        return Decimal(str(v))
    except:
        return Decimal("0")

def U(v):
    return ("" if v is None else str(v)).strip().upper()

def S(v):
    return ("" if v is None else str(v)).strip()


def obtener_faltante_resumen(faltantes):
    """
    Devuelve UN SOLO componente faltante y su cantidad acumulada.
    Evita concatenar múltiples códigos en U_PF_CFAL.
    """
    if not faltantes:
        return "", ""

    comp = next(iter(faltantes))
    cantidad = faltantes.get(comp, D(0))
    return comp, str(float(cantidad))

def obtener_usuario(argumentos=None):
    parser = argparse.ArgumentParser(description="Armar preoferta y registrar consumos por solicitud.")
    parser.add_argument("codigo_usuario", nargs="?", help="Código de usuario SAP")
    parser.add_argument("--usuario", dest="usuario", help="Alternativa al parámetro posicional")
    args = parser.parse_args(argumentos)
    if args.codigo_usuario and args.usuario and S(args.codigo_usuario) != S(args.usuario):
        parser.error("Los dos parámetros de usuario deben coincidir.")
    usuario = S(args.usuario or args.codigo_usuario)
    if not usuario:
        parser.error("Debe indicar el código de usuario: mainPO.exe manager")
    return usuario


USUARIO = obtener_usuario()
import Sub_SAP

# =====================================================
# CONFIG
# =====================================================
def resource_path(relative_path):
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)



# =====================================================
# OBTENER RUTA DEL ARCHIVO (FUNCIONA EN .PY Y .EXE)
# =====================================================
def obtener_ruta_archivo(nombre_archivo):

    if getattr(sys, 'frozen', False):
        # Cuando es ejecutable
        ruta_base = os.path.dirname(sys.executable)
    else:
        # Cuando es script Python
        ruta_base = os.path.dirname(os.path.abspath(__file__))

    return os.path.join(ruta_base, nombre_archivo)


# =====================================================
# CARGAR CONFIG
# =====================================================
ruta_config = obtener_ruta_archivo("config.json")

with open(ruta_config, "r", encoding="utf-8") as f:
    cfg = json.load(f)["Configuracion"]


cn = pyodbc.connect(
    f"DSN={cfg['dns']};UID={cfg['user']};PWD={cfg['password']}",
    autocommit=True
)
cur = cn.cursor()

# =====================================================
# SOLICITUD (CAB)
# =====================================================
# Leer el origen una vez y agrupar como HT_PREOFERTA_CAB.
# Solo una oferta informada excluye la línea; la cabecera sin oferta se regenera.
cur.execute(f'''
SELECT
    CASE WHEN W."U_PF_CC1" = 'PF' THEN 'A'
         WHEN W."U_PF_CC1" = 'MK' THEN 'B'
         WHEN W."U_PF_CC1" = 'VP' THEN 'C'
         WHEN W."U_PF_CC1" = 'NA' THEN 'D'
         ELSE 'OTRO' END AS "Serie",
    H."DocEntry", H."DocNum", L."LineNum", L."ItemCode", L."Dscription",
    I."CardCode" AS "Cod_Prov", P."CardName" AS "Proveedor",
    CASE WHEN UPPER(TRIM(I."BuyUnitMsr")) = 'SETSX5'
         THEN 'SI' ELSE 'NO' END AS "Es_Set",
    L."Quantity" AS "Cantidad_Planificada_Original",
    CASE WHEN UPPER(TRIM(I."BuyUnitMsr")) = 'SETSX5'
         THEN L."Quantity" * I."NumInBuy"
         ELSE L."Quantity" END AS "Cantidad_Planificada"
FROM "{cfg['schema']}"."OPRQ" H
INNER JOIN "{cfg['schema']}"."PRQ1" L ON H."DocEntry" = L."DocEntry"
INNER JOIN "{cfg['schema']}"."OITM" I ON I."ItemCode" = L."ItemCode"
LEFT JOIN "{cfg['schema']}"."OCRD" P ON P."CardCode" = I."CardCode"
LEFT JOIN "{cfg['schema']}"."OWHS" W ON W."WhsCode" = L."WhsCode"
WHERE H."CANCELED" = 'N'
  AND H."DocStatus" <> 'C'
  AND H."DocType" = 'I'
  AND I."ItemType" <> 'F'
  AND I."U_PF_TIP" = 'I'
  AND NOT EXISTS (
      SELECT 1 FROM "{cfg['schema']}"."@PF_CAB_PREOFERTA" X
      WHERE X."U_PF_SOL" = TO_NVARCHAR(H."DocEntry")
        AND X."U_PF_LIN" = TO_NVARCHAR(L."LineNum")
        AND IFNULL(TRIM(X."U_PF_OFE"), '') <> ''
  )
ORDER BY H."DocEntry", L."LineNum", L."ItemCode"
''')
origen_solicitudes = cur.fetchall()

agrupados_cab = {}
for r in origen_solicitudes:
    clave = (S(r.Serie), S(r.ItemCode), S(r.Dscription),
             S(r.Cod_Prov), S(r.Proveedor), S(r.Es_Set))
    if clave not in agrupados_cab:
        agrupados_cab[clave] = SimpleNamespace(
            Serie=clave[0], ItemCode=clave[1], Dscription=clave[2],
            DocNums="", LineNums="", Cantidad_Planificada=D(0),
            Cod_Prov=clave[3], Proveedor=clave[4], Es_Set=clave[5],
            Cantidad_Planificada_Original=D(0)
        )
    g = agrupados_cab[clave]
    g.DocNums += (" / " if g.DocNums else "") + str(r.DocEntry)
    g.LineNums += (" / " if g.LineNums else "") + str(r.LineNum)
    g.Cantidad_Planificada += D(r.Cantidad_Planificada)
    g.Cantidad_Planificada_Original += D(r.Cantidad_Planificada_Original)
sol = sorted(agrupados_cab.values(), key=lambda r: (r.Serie, r.DocNums, r.ItemCode))

# =====================================================
# LISTA DE MATERIALES (DETV2)
# =====================================================
cur.execute(f'''
SELECT
    "ItemPadre",
    "ItemComponente",
    "Descripcion",
    "Cantidad",
    "Pedir_Padre",
    "Armar_Padre",
    "Pedir_Comp",
    "Componente_Compra",
    "Padre_Compra",
    "CategoriaPadre",
    "ComponenteP"
FROM "{cfg['schema']}"."HT_PREOFERTA_DETV2"
''')
lm = cur.fetchall()



cur.close()
cn.close()

# =====================================================
# INDEXAR LM + PROPIEDADES DE COMPRA
# =====================================================
lm_padre = {}                 # padre -> list({item, qty, pedir_comp, componente_compra})
padres_por_componente = {}    # comp -> set(padres)
props_padre = {}              # padre -> {"armar","pedir","compra"}
compra_por_item = {}          # item -> SI/NO
pedir_comp_por_item = {}      # item -> SI/NO
categoria_padre = {}          # ItemPadre -> CategoriaPadre


def guardar_categoria(destino, item, valor, campo):
    """Conserva la categoría informada y detecta valores contradictorios."""
    if not item or valor is None or str(valor).strip() == "":
        return
    try:
        categoria = Decimal(str(valor).strip())
        if not categoria.is_finite():
            raise ValueError("categoría no finita")
    except (ValueError, ArithmeticError):
        raise ValueError(f"Categoría inválida en {campo} para {item}: {valor!r}")
    anterior = destino.get(item)
    if anterior is not None and anterior != categoria:
        raise ValueError(
            f"Categorías contradictorias en {campo} para {item}: "
            f"{anterior} y {categoria}. Revisar HT_PREOFERTA_DETV2."
        )
    destino[item] = categoria

for r in lm:
    padre = S(getattr(r, "ItemPadre", ""))
    comp  = S(getattr(r, "ItemComponente", ""))

    pedir_padre = U(getattr(r, "Pedir_Padre", "NO"))
    armar_padre = U(getattr(r, "Armar_Padre", "NO"))
    pedir_comp  = U(getattr(r, "Pedir_Comp", "NO"))
    componente_compra = U(getattr(r, "Componente_Compra", "NO"))
    padre_compra = U(getattr(r, "Padre_Compra", "NO"))

    guardar_categoria(categoria_padre, padre, getattr(r, "CategoriaPadre", None), "CategoriaPadre")

    if padre:
        if padre not in props_padre:
            props_padre[padre] = {
                "armar": armar_padre if armar_padre else "NO",
                "pedir": pedir_padre if pedir_padre else "NO",
                "compra": padre_compra if padre_compra else "NO"
            }
        compra_por_item[padre] = padre_compra if padre_compra else compra_por_item.get(padre, "NO")

        # Para filas sin componente, usamos Pedir_Comp como propiedad del artículo enlistado.
        if not comp:
            pedir_comp_por_item[padre] = pedir_comp if pedir_comp else "NO"

    if padre and comp:
        lm_padre.setdefault(padre, []).append({
            "item": comp,
            "qty": D(getattr(r, "Cantidad", 0)),
            "desc": S(getattr(r, "Descripcion", "")),
            "componente_p": U(getattr(r, "ComponenteP", "")),
            "pedir_comp": pedir_comp if pedir_comp else "NO",
            "componente_compra": componente_compra if componente_compra else "NO"
        })

        padres_por_componente.setdefault(comp, set()).add(padre)
        compra_por_item[comp] = componente_compra if componente_compra else "NO"
        pedir_comp_por_item[comp] = pedir_comp if pedir_comp else "NO"


def compra_padre(padre):
    return U(props_padre.get(padre, {}).get("compra", "NO")) or "NO"


def compra_componentes_padre(padre):
    for c in lm_padre.get(padre, []):
        if U(c.get("componente_compra", "NO")) == "SI":
            return "SI"
    return "NO"


def es_articulo_compra_sin_lm(item):
    """
    Nuevo escenario:
    - Armar silla = NO
    - Pedir componente = SI
    - Artículo de compra = SI
    - No tiene lista de materiales
    """
    armar = U(props_padre.get(item, {}).get("armar", "NO"))
    pedir_comp = U(pedir_comp_por_item.get(item, "NO"))
    compra = U(compra_por_item.get(item, props_padre.get(item, {}).get("compra", "NO")))
    tiene_lm = bool(lm_padre.get(item, []))

    return (
        armar == "NO"
        and pedir_comp == "SI"
        and compra == "SI"
        and not tiene_lm
    )

# Caso 15: silla de compra con LM que no se arma ni pide componentes.
OBS_CASO_15 = "COMPONENTES SILLA COMPRA (ARMAR NO / PEDIR COMP NO)"


def es_silla_compra_con_lm(item):
    comps = lm_padre.get(item, [])
    return (
        bool(comps)
        and U(props_padre.get(item, {}).get("armar", "NO")) == "NO"
        and compra_padre(item) == "SI"
        and all(U(c.get("pedir_comp", "NO")) == "NO" for c in comps)
    )


def componentes_para_mostrar(data):
    """Desglosa solo el caso 15; conserva cantidades y metadatos de CAB.

    Si CAB ya contiene componentes, se muestran sus cantidades originales.
    Si CAB contiene la silla, se desglosa según cantidad de la LM.
    Este desglose es informativo: no consume stock ni fabrica sillas.
    """
    for fila in data.get("componentes", []):
        item = fila["item"]
        if es_silla_compra_con_lm(item):
            for c in sorted(lm_padre[item], key=lambda c: clave_categoria_componente(c["item"])):
                if c["qty"] <= 0:
                    continue
                yield dict(fila, item=c["item"], plan=D(fila["plan"]) * c["qty"],
                           caso_15=True, padre_caso_15=item,
                           item_origen=item, cantidad_origen=D(fila["plan"]))
        else:
            padres_caso = sorted(
                (padre for padre in padres_por_componente.get(item, set())
                 if es_silla_compra_con_lm(padre)), key=clave_categoria_padre
            )
            yield dict(fila, caso_15=bool(padres_caso),
                       padre_caso_15=" / ".join(padres_caso))


# =====================================================
# STOCK INICIAL (desde CAB) + DOCS por SERIE/ITEM + datos extra CAB
# =====================================================
stock = {}                   # item -> disponibilidad (suma planificada en CAB)
docs_item_serie = {}         # (serie,item) -> set(docnums)
cab_info_item_serie = {}     # (serie,item) -> {"line","cod_prov","proveedor"}

for r in sol:
    serie = S(getattr(r, "Serie", ""))
    item  = S(getattr(r, "ItemCode", ""))
    plan  = D(getattr(r, "Cantidad_Planificada", 0))

    stock[item] = stock.get(item, D(0)) + plan
    docs_item_serie.setdefault((serie, item), set()).add(S(getattr(r, "DocNums", "")))

    key = (serie, item)
    if key not in cab_info_item_serie:
        cab_info_item_serie[key] = {
            "line": S(getattr(r, "LineNums", "")),
            "cod_prov": S(getattr(r, "Cod_Prov", "")),
            "proveedor": S(getattr(r, "Proveedor", ""))
        }

# Trazabilidad del stock: no se infiere a partir de DocNums agregados.
def decimal_texto(valor):
    valor = D(valor)
    if not valor.is_finite():
        raise ValueError("Cantidad no finita")
    cadena = format(valor, "f")
    return cadena.rstrip("0").rstrip(".") if "." in cadena else cadena


def crear_origenes(filas_cab, filas_origen):
    esperado = {}
    for r in filas_cab:
        clave = (S(r.Serie), S(r.ItemCode))
        esperado[clave] = esperado.get(clave, D(0)) + D(r.Cantidad_Planificada)
    encontrado = {}
    lotes = {}
    claves = set()
    for r in sorted(filas_origen, key=lambda r: (int(r.DocEntry), int(r.LineNum), S(r.ItemCode))):
        cantidad = D(r.Cantidad_Planificada)
        if not cantidad.is_finite() or cantidad < 0:
            raise ValueError(f"Cantidad de origen inválida: {r.DocEntry}/{r.LineNum}")
        clave_linea = (int(r.DocEntry), int(r.LineNum))
        if clave_linea in claves:
            raise ValueError(f"Línea de origen duplicada: {clave_linea}")
        claves.add(clave_linea)
        clave = (S(r.Serie), S(r.ItemCode))
        encontrado[clave] = encontrado.get(clave, D(0)) + cantidad
        lotes.setdefault(S(r.ItemCode), []).append({
            "serie": S(r.Serie), "solicitud": str(r.DocEntry),
            "linea": str(r.LineNum), "item": S(r.ItemCode), "saldo": cantidad
        })
    for clave in set(esperado) | set(encontrado):
        if esperado.get(clave, D(0)) != encontrado.get(clave, D(0)):
            raise ValueError(
                f"CAB y solicitudes no coinciden para {clave}: "
                f"CAB={esperado.get(clave, 0)}, origen={encontrado.get(clave, 0)}. "
                "Vuelva a ejecutar con los datos estables."
            )
    return lotes


origenes_stock = crear_origenes(sol, origen_solicitudes)


def consumir_stock(item, cantidad, eventos):
    """Descuenta exactamente la cantidad ya decidida por el armado original."""
    cantidad = D(cantidad)
    if cantidad <= 0:
        return
    lotes = origenes_stock.get(item, [])
    if cantidad > stock.get(item, D(0)) or cantidad > sum((x["saldo"] for x in lotes), D(0)):
        raise ValueError(f"Consumo sin saldo de origen: {item}, {cantidad}")
    pendiente = cantidad
    for lote in lotes:
        tomar = min(lote["saldo"], pendiente)
        if tomar > 0:
            lote["saldo"] -= tomar
            eventos.append({
                "serie": lote["serie"], "solicitud": lote["solicitud"],
                "linea": lote["linea"], "item": item, "cantidad": tomar
            })
            pendiente -= tomar
        if pendiente == 0:
            break
    stock[item] = stock.get(item, D(0)) - cantidad


def campos_consumo(eventos, serie):
    """Cuatro listas paralelas, una posición por solicitud/línea/componente."""
    agrupado = {}
    for e in eventos:
        if e["serie"] != serie or e["cantidad"] <= 0:
            continue
        clave = (e["solicitud"], e["linea"], e["item"])
        agrupado[clave] = agrupado.get(clave, D(0)) + e["cantidad"]
    claves = sorted(agrupado, key=lambda k: (int(k[0]), int(k[1]), k[2]))
    return {
        "PF_SOL": " / ".join(k[0] for k in claves),
        "PF_LINEA": " / ".join(k[1] for k in claves),
        "PF_COMCONS": " / ".join(k[2] for k in claves),
        "PF_CCON": " / ".join(decimal_texto(agrupado[k]) for k in claves),
        "PF_USR": USUARIO
    }


# Artículos que aparecen en CAB, incluso si su cantidad es cero.
items_en_solicitud = {S(getattr(r, "ItemCode", "")) for r in sol}

# =====================================================
# PLAN POR PADRE
# =====================================================
# REGLAS:
# - Pedir_Padre = SI  -> solo completas -> MIN capacidad
# - Pedir_Padre = NO  -> permite incompletas
#   * Si existen componentes Pedir_Comp = SI, esos componentes
#     NO pueden faltar y limitan el total.
#   * Si todos los componentes tienen Pedir_Comp = NO, se intenta
#     aprovechar hasta la mayor capacidad disponible (MAX).
# =====================================================
plan_padre = {}

for padre, comps in lm_padre.items():
    capacidades_todas = []
    capacidades_obligatorias = []

    for c in comps:
        qty = c["qty"]
        if qty <= 0:
            continue

        comp = c["item"]
        pedir_comp = U(c.get("pedir_comp", "NO"))
        disp = stock.get(comp, D(0))
        capacidad = disp // qty

        capacidades_todas.append(capacidad)

        if pedir_comp == "SI":
            capacidades_obligatorias.append(capacidad)

    pedir_padre = U(props_padre.get(padre, {}).get("pedir", "NO"))

    if not capacidades_todas:
        plan_padre[padre] = D(0)
    elif pedir_padre == "SI":
        plan_padre[padre] = min(capacidades_todas)
    elif capacidades_obligatorias:
        plan_padre[padre] = min(capacidades_obligatorias)
    else:
        plan_padre[padre] = max(capacidades_todas)

# =====================================================
# ORDENAR PADRES POR CATEGORÍA NUMÉRICA ASCENDENTE
# =====================================================
def capacidad_armado(padre):
    comps = lm_padre.get(padre, [])
    if not comps:
        return D(0)

    caps = []
    for c in comps:
        qty = c["qty"]
        if qty <= 0:
            continue
        disp = stock.get(c["item"], D(0))
        caps.append(disp // qty)

    return min(caps) if caps else D(0)

def clave_categoria_padre(item):
    # Categoría del padre exclusivamente: 100 antes que 101.
    categoria = categoria_padre.get(item)
    return (categoria is None, categoria if categoria is not None else D(0), item)


def clave_categoria_componente(item):
    # No se dispone de categoría del componente en DETV2.
    return (True, D(0), item)


def clave_categoria(item):
    # Padres usan CategoriaPadre; los demás se ordenan por código.
    if item in lm_padre or item in categoria_padre:
        return clave_categoria_padre(item)
    return clave_categoria_componente(item)


orden_padres = sorted(lm_padre.keys(), key=clave_categoria_padre)

# =====================================================
# PRODUCCIÓN POR PADRE (GLOBAL)
# =====================================================
produccion = {}

def fabricar_padre(padre, fase):

    comps = [c for c in lm_padre.get(padre, []) if c["qty"] > 0]

    armar_padre = U(props_padre.get(padre, {}).get("armar", "NO"))
    pedir_padre = U(props_padre.get(padre, {}).get("pedir", "NO"))

    resultado_vacio = {
        "completas": D(0),
        "incompletas": D(0),
        "consumed_comps": set(),
        "faltantes": {},
        "sobrantes": {},
        "filas": []
    }

    if armar_padre != "SI" or not comps:
        produccion[padre] = resultado_vacio
        return produccion[padre]

    previo = produccion.get(padre, resultado_vacio)
    completas = previo["completas"]
    incompletas = previo["incompletas"]
    consumed_comps = previo["consumed_comps"]
    faltantes_total = previo["faltantes"]
    sobrantes = {}
    filas = previo["filas"]

    # =====================================================
    # 1) COMPLETAS
    # Respeta Cantidad de LM por componente
    # =====================================================
    if fase == "COMPLETAS":
        capacidades = []

        for c in comps:
            comp = c["item"]
            qty = c["qty"]
            disp = stock.get(comp, D(0))
            capacidades.append(disp // qty)

        completas = min(capacidades) if capacidades else D(0)

        if completas > 0:
            eventos_lote = []
            for c in comps:
                comp = c["item"]
                qty = c["qty"]
                consumo = qty * completas

                consumir_stock(comp, consumo, eventos_lote)

                if consumo > 0:
                    consumed_comps.add(comp)

            filas.append({
                "consumos": eventos_lote,
                "cantidad": completas,
                "completas": completas,
                "incompletas": D(0),
                "faltantes": {},
                "tipo": "SOLO COMPLETAS"
            })

    # =====================================================
    # 2) INCOMPLETAS POR ETAPAS
    #
    # Fila 2: falta 1 componente
    # Fila 3: faltan 2 componentes
    # Fila 4: faltan 3 componentes
    # ... hasta terminar stock aprovechable.
    #
    # Siempre se respeta qty de la LM.
    # =====================================================
    componentes_pr = [c for c in comps if U(c.get("componente_p", "")) == "PR"]
    if fase == "INCOMPLETAS" and (
        componentes_pr or pedir_padre == "NO"
        or any(c["item"] not in items_en_solicitud for c in comps)
    ):

        while True:
            # Un componente PR nunca puede faltar. La LM define cuántos
            # se necesitan por silla; sin cantidad suficiente se detiene.
            if any(stock.get(c["item"], D(0)) < c["qty"] for c in componentes_pr):
                break

            # Componentes que ya no alcanzan para UNA silla
            componentes_faltantes = []

            for c in comps:
                comp = c["item"]
                qty = c["qty"]
                disp = stock.get(comp, D(0))

                if disp < qty:
                    componentes_faltantes.append(c)

            # Si ningún componente falta todavía, consume nuevas completas.
            # Es un seguro por si el stock cambió por componentes compartidos.
            if not componentes_faltantes:

                capacidades = []

                for c in comps:
                    comp = c["item"]
                    qty = c["qty"]
                    disp = stock.get(comp, D(0))
                    capacidades.append(disp // qty)

                lote = min(capacidades) if capacidades else D(0)

                if lote <= 0:
                    break

                eventos_lote = []
                for c in comps:
                    comp = c["item"]
                    qty = c["qty"]
                    consumo = qty * lote

                    consumir_stock(comp, consumo, eventos_lote)

                    if consumo > 0:
                        consumed_comps.add(comp)

                completas += lote

                if filas and not filas[-1]["faltantes"]:
                    filas[-1]["cantidad"] += lote
                    filas[-1]["completas"] += lote
                    filas[-1].setdefault("consumos", []).extend(eventos_lote)
                else:
                    filas.append({
                        "consumos": eventos_lote,
                        "cantidad": lote,
                        "completas": lote,
                        "incompletas": D(0),
                        "faltantes": {},
                        "tipo": "SOLO COMPLETAS"
                    })

                continue

            # Con PR, solo los componentes principales limitan el armado.
            # Los restantes se registran como faltantes, incluso si tienen
            # Pedir_Comp = SI. Sin PR se conserva la validación anterior.
            if not componentes_pr and any(
                c["item"] in items_en_solicitud and (
                    pedir_padre == "SI"
                    or U(c.get("pedir_comp", "NO")) == "SI"
                )
                for c in componentes_faltantes
            ):
                break
            if pedir_padre == "SI" and not componentes_pr and not any(
                c["item"] not in items_en_solicitud for c in componentes_faltantes
            ):
                break

            codigos_faltantes = {c["item"] for c in componentes_faltantes}

            # Capacidad de los componentes que SI siguen presentes.
            # Aquí se respeta cantidad LM.
            capacidades_presentes = []

            for c in comps:

                if c["item"] in codigos_faltantes:
                    continue

                comp = c["item"]
                qty = c["qty"]
                disp = stock.get(comp, D(0))

                capacidad = disp // qty

                if capacidad > 0:
                    capacidades_presentes.append(capacidad)

            # Si ya todos faltan, no se crea una silla vacía.
            if not capacidades_presentes:
                break

            # El lote termina cuando se agota el siguiente componente.
            lote = min(capacidades_presentes)

            if lote <= 0:
                break

            faltantes_fila = {}
            consumio_algo = False
            eventos_lote = []

            for c in comps:

                comp = c["item"]
                qty = c["qty"]
                disponible = stock.get(comp, D(0))
                requerido = qty * lote

                if comp in codigos_faltantes:
                    # Consume cualquier remanente parcial.
                    consumir = min(disponible, requerido)
                    faltante = requerido - consumir

                    consumir_stock(comp, consumir, eventos_lote)

                    if consumir > 0:
                        consumed_comps.add(comp)
                        consumio_algo = True

                    if faltante > 0:
                        faltantes_fila[comp] = faltante
                        faltantes_total[comp] = (
                            faltantes_total.get(comp, D(0)) + faltante
                        )

                else:
                    # Componente presente: debe cubrir TODO el lote
                    # según la cantidad de la LM.
                    consumir = requerido
                    consumir_stock(comp, consumir, eventos_lote)

                    if consumir > 0:
                        consumed_comps.add(comp)
                        consumio_algo = True

            if not consumio_algo:
                break

            incompletas += lote

            filas.append({
                "consumos": eventos_lote,
                "cantidad": lote,
                "completas": D(0),
                "incompletas": lote,
                "faltantes": faltantes_fila,
                "tipo": "INCOMPLETA"
            })

    produccion[padre] = {
        "completas": completas,
        "incompletas": incompletas,
        "consumed_comps": consumed_comps,
        "faltantes": faltantes_total,
        "sobrantes": sobrantes,
        "filas": filas
    }

    return produccion[padre]

# Prioridad absoluta por categoría del padre: terminar la categoría 101
# antes de asignar componentes a la 115. Dentro de cada categoría,
# fabricar primero todas las completas y después las incompletas.
padres_por_categoria = {}
for p in orden_padres:
    padres_por_categoria.setdefault(clave_categoria_padre(p)[:2], []).append(p)

for padres_categoria in padres_por_categoria.values():
    for fase in ("COMPLETAS", "INCOMPLETAS"):
        for p in padres_categoria:
            fabricar_padre(p, fase)

# Calcular sobrantes con el stock final de ambas pasadas.
for padre in orden_padres:
    if (U(props_padre.get(padre, {}).get("armar", "NO")) == "SI"
            and U(props_padre.get(padre, {}).get("pedir", "NO")) == "SI"):
        for c in lm_padre.get(padre, []):
            comp = c["item"]
            disp = stock.get(comp, D(0))
            if U(c.get("pedir_comp", "NO")) == "SI" and disp > 0:
                produccion[padre]["sobrantes"][comp] = disp

def separar_valores(valor):
    return [parte.strip() for parte in S(valor).split(" / ") if parte.strip()]


def clave_natural(valor):
    import re
    return tuple((0, int(parte)) if parte.isdigit() else (1, parte.upper())
                 for parte in re.split(r"(\d+)", S(valor)) if parte)


def ordenar_solicitudes_y_lineas(filas):
    """Alinea los números de solicitud y sus líneas sin ordenarlos por separado."""
    por_solicitud = {}
    for fila in filas:
        solicitudes = separar_valores(fila.get("docs", ""))
        lineas = separar_valores(fila.get("line", ""))
        if not solicitudes:
            continue
        if len(solicitudes) == 1:
            pares = [(solicitudes[0], linea) for linea in lineas] or [(solicitudes[0], "")]
        elif len(solicitudes) == len(lineas):
            pares = list(zip(solicitudes, lineas))
        elif not lineas:
            pares = [(doc, "") for doc in solicitudes]
        else:
            raise ValueError(
                "No se puede relacionar solicitud y línea de HT_PREOFERTA_CAB: "
                f"solicitudes={fila.get('docs')!r}, líneas={fila.get('line')!r}. "
                "La vista debe devolver los valores en el mismo orden."
            )
        for doc, linea in pares:
            por_solicitud.setdefault(doc, set())
            if linea:
                por_solicitud[doc].add(linea)
    docs = sorted(por_solicitud, key=clave_natural)
    # Un grupo de líneas por solicitud; conservar una posición incluso si está vacía.
    lineas = [", ".join(sorted(por_solicitud[doc], key=clave_natural)) for doc in docs]
    return " / ".join(docs), " / ".join(lineas)


# =====================================================
# DETALLE POR SERIE (para UI)  -> SOLO USAREMOS PARA RESUMEN
# =====================================================
detalle_por_serie = {}  # serie -> {"padres":{}, "componentes":[], "solicitud":""}

for r in sol:
    serie = S(getattr(r, "Serie", ""))
    item  = S(getattr(r, "ItemCode", ""))
    plan  = D(getattr(r, "Cantidad_Planificada", 0))

    docs = S(getattr(r, "DocNums", ""))
    docs_set = set(separar_valores(docs))
    extra = {
        "line": S(getattr(r, "LineNums", "")),
        "cod_prov": S(getattr(r, "Cod_Prov", "")),
        "proveedor": S(getattr(r, "Proveedor", ""))
    }

    detalle_por_serie.setdefault(serie, {"padres": {}, "componentes": [], "solicitud": ""})

    # solicitud concatenada por serie
    if docs:
        if not detalle_por_serie[serie]["solicitud"]:
            detalle_por_serie[serie]["solicitud"] = docs
        else:
            existentes = set(detalle_por_serie[serie]["solicitud"].split(" / "))
            existentes.update(docs.split(" / "))
            detalle_por_serie[serie]["solicitud"] = " / ".join(sorted({x for x in existentes if x}))

    # Si el item es componente de algún padre, enlazar padre en la serie
    if item in padres_por_componente:
        for padre in sorted(padres_por_componente[item], key=clave_categoria_padre):
            detalle_por_serie[serie]["padres"].setdefault(padre, {
                "docs": set(),
                "plan": plan_padre.get(padre, D(0)),
                "prod": produccion.get(padre, {"completas": D(0), "incompletas": D(0), "faltantes": {}, "sobrantes": {}, "filas": []}),
            })
            for d in docs_set:
                if d:
                    detalle_por_serie[serie]["padres"][padre]["docs"].add(str(d))

    # COMPONENTE de la solicitud (CAB)
    detalle_por_serie[serie]["componentes"].append({
        "item": item,
        "plan": plan,
        "docs": docs,
        "line": extra.get("line", ""),
        "cod_prov": extra.get("cod_prov", ""),
        "proveedor": extra.get("proveedor", "")
    })

# =====================================================
# RESUMEN UI (UNA SOLA PANTALLA)
# - agrega LineNums, Cod_Prov, Proveedor después de Serie
# - quita Componentes_Padre y BOM_Qty
# - en su lugar pone Componente_Solicitud
# =====================================================
root = tk.Tk()
root.title("Resumen – Armado por Serie")
root.geometry("2300x780")

cols = (
    "Serie",
    "LineNums",
    "Cod_Prov",
    "Proveedor",
    "Solicitud",
    "Padre",
    "Componente_Solicitud",
    "Cant_Planificada",
    "Completas",
    "Incompletas",
    "Total",
    "Comp_Faltante",
    "Cant_Faltante",
    "Tipo",
    "Componente_Compra",
    "Padre_Compra"
)

frame_tree = tk.Frame(root)
frame_tree.pack(fill=tk.BOTH, expand=True)

scroll_y = tk.Scrollbar(frame_tree, orient="vertical")
scroll_y.pack(side=tk.RIGHT, fill=tk.Y)

scroll_x = tk.Scrollbar(frame_tree, orient="horizontal")
scroll_x.pack(side=tk.BOTTOM, fill=tk.X)

tr = ttk.Treeview(
    frame_tree,
    columns=cols,
    show="headings",
    yscrollcommand=scroll_y.set,
    xscrollcommand=scroll_x.set
)

scroll_y.config(command=tr.yview)
scroll_x.config(command=tr.xview)

tr.pack(fill=tk.BOTH, expand=True)

for c in cols:
    tr.heading(c, text=c)
    tr.column(c, width=170, anchor=tk.W)

# =====================================================
# COMPONENTES USADOS POR SERIE (para no duplicar sueltos)
# Usamos consumed_comps (set) para detectar qué componentes fueron consumidos
# =====================================================
usados_por_serie = {s: set() for s in detalle_por_serie.keys()}

for serie, data in detalle_por_serie.items():
    for padre, info in sorted(data.get("padres", {}).items(), key=lambda par: clave_categoria_padre(par[0])):
        prod = info["prod"]
        # FIX: consumed_comps es un set directo, sustituye iteraciones[]
        usados_por_serie[serie].update(prod.get("consumed_comps", set()))

        # sobrantes también cuentan como usados para no duplicar como “suelto”
        for comp in prod.get("sobrantes", {}).keys():
            usados_por_serie[serie].add(comp)

# Para no repetir sobrantes por serie
sobrantes_mostrados_por_serie = {s: set() for s in detalle_por_serie.keys()}

# =====================================================
# RECORRER SERIES
# =====================================================
for serie, data in detalle_por_serie.items():

    solicitud_serie = data.get("solicitud", "")

    # ================= PADRES =================
    for padre, info in sorted(data.get("padres", {}).items(), key=lambda par: clave_categoria_padre(par[0])):

        prod = info["prod"]
        completas = prod.get("completas", D(0))
        incompletas = prod.get("incompletas", D(0))
        total = completas + incompletas
        plan = info.get("plan", D(0))

        armar = U(props_padre.get(padre, {}).get("armar", "NO"))
        pedir_padre = U(props_padre.get(padre, {}).get("pedir", "NO"))

        # Si no arma o no armó nada, NO mostrar padre
        if armar != "SI" or total <= 0:
            continue

        docs_padre = info.get("docs", set())
        solicitud = " / ".join(sorted(docs_padre)) if docs_padre else solicitud_serie


        # =========================================
        # AGRUPAR POR ITEM EN LA SERIE
        # =========================================
        line_set = set()
        codprov_set = set()
        prov_set = set()
        comp_set = set()

        # componentes BOM del padre
        comps_padre = lm_padre.get(padre, [])

        for comp_bom in comps_padre:
            comp_item = comp_bom.get("item")

            # buscar en CAB de la serie
            for comp_row in data.get("componentes", []):

                if comp_row.get("item") == comp_item:
                    comp_set.add(comp_item)
                    if comp_row.get("line"):
                        line_set.add(str(comp_row.get("line")))
                    if comp_row.get("cod_prov"):
                        codprov_set.add(str(comp_row.get("cod_prov")))
                    if comp_row.get("proveedor"):
                        prov_set.add(str(comp_row.get("proveedor")))


        filas_del_padre = [fila for fila in data.get("componentes", [])
                           if fila.get("item") in {c["item"] for c in comps_padre}]
        solicitud_ordenada, lineNums = ordenar_solicitudes_y_lineas(filas_del_padre)
        solicitud = solicitud_ordenada or solicitud
        cod_prov = " / ".join(sorted(codprov_set))
        proveedor = " / ".join(sorted(prov_set))
        componentes_solicitud_padre = " / ".join(sorted(comp_set))  # <-- NUEVO

        # Una fila visual por cada etapa de armado
        for etapa in prod.get("filas", []):

            faltantes_etapa = etapa.get("faltantes", {})

            comp_falt = " / ".join(
                faltantes_etapa.keys()
            ) if faltantes_etapa else ""

            cant_falt = " / ".join(
                str(float(v))
                for v in faltantes_etapa.values()
            ) if faltantes_etapa else ""

            cant_etapa = etapa.get("cantidad", D(0))
            completas_etapa = etapa.get("completas", D(0))
            incompletas_etapa = etapa.get("incompletas", D(0))
            tipo = etapa.get("tipo", "")

            trazabilidad = campos_consumo(etapa.get("consumos", []), serie)
            tr.insert("", tk.END, values=(
                serie,
                trazabilidad["PF_LINEA"] if trazabilidad["PF_COMCONS"] else lineNums,
                cod_prov,
                proveedor,
                trazabilidad["PF_SOL"] if trazabilidad["PF_COMCONS"] else solicitud,
                padre,
                componentes_solicitud_padre,
                float(cant_etapa),
                float(completas_etapa),
                float(incompletas_etapa),
                float(cant_etapa),
                comp_falt,
                cant_falt,
                tipo,
                compra_componentes_padre(padre),
                compra_padre(padre)
            ))

        # ================= SOBRANTES (solo una vez por serie y solo si el padre ARMÓ) =================
        for comp, cant in prod.get("sobrantes", {}).items():

            if comp in sobrantes_mostrados_por_serie[serie]:
                continue

            # Si el componente no está en la solicitud CAB de esa serie, NO lo muestres (según tu regla)
            comps_solicitud = {x["item"] for x in data.get("componentes", [])}
            if comp not in comps_solicitud:
                continue

            sobrantes_mostrados_por_serie[serie].add(comp)

            tr.insert("", tk.END, values=(
                serie,
                lineNums,
                cod_prov,
                proveedor,
                solicitud,
                #padre, padre del componente
                comp,
                comp,             # Componente_Solicitud
                0.0,
                0.0,
                0.0,
                float(cant),
                comp,
                float(cant),
                "COMPONENTE SOBRANTE (PEDIR COMP = SI)",
                U(compra_por_item.get(comp, "NO")),
                compra_padre(padre)
            ))

    # ================= COMPONENTES SUELTOS (solo CAB que NO participaron en armado) =================
    for comp in componentes_para_mostrar(data):

        item = comp["item"]
        plan = D(comp["plan"])
        docs = comp.get("docs", solicitud_serie)

        if item in usados_por_serie.get(serie, set()):
            continue

        docs, lineNums = ordenar_solicitudes_y_lineas([comp])
        docs = docs or comp.get("docs", solicitud_serie)
        cod_prov = comp.get("cod_prov", "")
        proveedor = comp.get("proveedor", "")

        # Si el item es padre en LM, se considera “PADRE COMPLETO SOLICITUD”
        if item in lm_padre and not comp.get("caso_15"):
            tipo = "PADRE COMPLETO SOLICITUD"
            completas_item = plan
            incompletas_item = D(0)
            total_item = plan
            tr.insert("", tk.END, values=(
                serie,
                lineNums,
                cod_prov,
                proveedor,
                docs,
                item,
                item,
                float(plan),
                float(completas_item),
                float(incompletas_item),
                float(total_item),
                "",
                "",
                tipo,
                "NO",
                compra_padre(item)
            ))

        elif es_articulo_compra_sin_lm(item) and not comp.get("caso_15"):
            tipo = "ARTICULO DE COMPRA"
            completas_item = D(0)
            incompletas_item = D(0)
            total_item = plan

            tr.insert("", tk.END, values=(
                serie,
                lineNums,
                cod_prov,
                proveedor,
                docs,
                item,
                item,
                float(plan),
                0.0,
                0.0,
                float(total_item),
                "",
                "",
                tipo,
                "SI",
                ("SI" if comp.get("caso_15") else U(props_padre.get(item, {}).get("compra", "NO")))
            ))

        else:
            tipo = (OBS_CASO_15 if comp.get("caso_15") else "COMPONENTE SUELTO")
            completas_item = D(0)
            incompletas_item = D(0)
            total_item = plan

            tr.insert("", tk.END, values=(
                serie,
                lineNums,
                cod_prov,
                proveedor,
                docs,
                item,
                item,
                float(plan),
                0.0,
                0.0,
                float(total_item),
                "",
                "",
                tipo,
                U(compra_por_item.get(item, "NO")),
                ("SI" if comp.get("caso_15") else U(props_padre.get(item, {}).get("compra", "NO")))
            ))

        # =====================================================
# ARMAR DATOS PARA SAP DESDE EL TREEVIEW (PADRES + SOBRANTES + SUELTOS)
# =====================================================
datos_sap = []

# Reusar lo mismo que insertas en pantalla:
for serie, data in detalle_por_serie.items():

    solicitud_serie = data.get("solicitud", "")

    # -------------- PADRES y SOBRANTES --------------
    sobrantes_mostrados = set()

    for padre, info in sorted(data.get("padres", {}).items(), key=lambda par: clave_categoria_padre(par[0])):

        prod = info["prod"]
        completas = prod.get("completas", D(0))
        incompletas = prod.get("incompletas", D(0))
        total = completas + incompletas
        plan = info.get("plan", D(0))

        armar = U(props_padre.get(padre, {}).get("armar", "NO"))
        if armar != "SI" or total <= 0:
            continue

        docs_padre = info.get("docs", set())
        solicitud = " / ".join(sorted(docs_padre)) if docs_padre else solicitud_serie

        # ---- armar lineNums/codprov/proveedor + componentes_solicitud_padre (FIX A)
        line_set = set()
        codprov_set = set()
        prov_set = set()
        comp_set = set()

        comps_padre = lm_padre.get(padre, [])
        for comp_bom in comps_padre:
            comp_item = comp_bom.get("item")
            for comp_row in data.get("componentes", []):
                if comp_row.get("item") == comp_item:
                    comp_set.add(comp_item)
                    if comp_row.get("line"):
                        line_set.add(str(comp_row.get("line")))
                    if comp_row.get("cod_prov"):
                        codprov_set.add(str(comp_row.get("cod_prov")))
                    if comp_row.get("proveedor"):
                        prov_set.add(str(comp_row.get("proveedor")))

        filas_del_padre = [fila for fila in data.get("componentes", [])
                           if fila.get("item") in {c["item"] for c in comps_padre}]
        solicitud_ordenada, lineNums = ordenar_solicitudes_y_lineas(filas_del_padre)
        solicitud = solicitud_ordenada or solicitud
        cod_prov = " / ".join(sorted(codprov_set))
        proveedor = " / ".join(sorted(prov_set))
        componentes_solicitud_padre = " / ".join(sorted(comp_set))

        # Una fila SAP por cada etapa de armado
        for etapa in prod.get("filas", []):

            faltantes_etapa = etapa.get("faltantes", {})

            comp_falt = " / ".join(
                faltantes_etapa.keys()
            ) if faltantes_etapa else ""

            cant_falt = " / ".join(
                str(float(v))
                for v in faltantes_etapa.values()
            ) if faltantes_etapa else ""

            cant_etapa = etapa.get("cantidad", D(0))
            completas_etapa = etapa.get("completas", D(0))
            incompletas_etapa = etapa.get("incompletas", D(0))
            tipo = etapa.get("tipo", "")

            datos_sap.append({
                "PF_SERIE": serie,
                "PF_LINEA": lineNums,
                "PF_COD_PROV": cod_prov,
                "PF_PROVEEDOR": proveedor,
                "PF_SOL": solicitud,
                "PF_COMP": componentes_solicitud_padre,
                "PF_PADRE": padre,
                "PF_CANT": float(cant_etapa),
                "PF_COM": float(completas_etapa),
                "PF_INC": float(incompletas_etapa),
                "PF_TOT": float(cant_etapa),
                "PF_CFAL": comp_falt,
                "PF_CAFAL": cant_falt,
                "PF_TIPO": tipo,
                "PF_CCOMPRA": compra_componentes_padre(padre),
                "PF_PCOMPRA": compra_padre(padre)
            })
            trazabilidad = campos_consumo(etapa.get("consumos", []), serie)
            datos_sap[-1].update({
                "PF_COMCONS": trazabilidad["PF_COMCONS"],
                "PF_CCON": trazabilidad["PF_CCON"],
                "PF_USR": USUARIO
            })
            if trazabilidad["PF_COMCONS"]:
                datos_sap[-1]["PF_SOL"] = trazabilidad["PF_SOL"]
                datos_sap[-1]["PF_LINEA"] = trazabilidad["PF_LINEA"]

        # ---- SOBRANTES a SAP
        comps_solicitud = {x["item"] for x in data.get("componentes", [])}
        for comp, cant in prod.get("sobrantes", {}).items():

            if comp in sobrantes_mostrados:
                continue
            if comp not in comps_solicitud:
                continue

            sobrantes_mostrados.add(comp)

            datos_sap.append({
                "PF_SERIE": serie,
                "PF_LINEA": lineNums,
                "PF_COD_PROV": cod_prov,
                "PF_PROVEEDOR": proveedor,
                "PF_SOL": solicitud,
                "PF_COMP": comp,                # aquí sí es el componente puntual
                "PF_PADRE": comp,
                "PF_CANT": 0.0,
                "PF_COM": 0.0,
                "PF_INC": 0.0,
                "PF_TOT": float(cant),
                "PF_CFAL": comp,
                "PF_CAFAL": float(cant),
                "PF_TIPO": "COMPONENTE SOBRANTE (PEDIR COMP = SI)",
                "PF_CCOMPRA": U(compra_por_item.get(comp, "NO")),
                "PF_PCOMPRA": compra_padre(padre)
            })

    # -------------- COMPONENTES SUELTOS --------------
    for comp in componentes_para_mostrar(data):

        item = comp["item"]
        plan = D(comp["plan"])
        docs = comp.get("docs", solicitud_serie)

        if item in usados_por_serie.get(serie, set()):
            continue

        docs, lineNums = ordenar_solicitudes_y_lineas([comp])
        docs = docs or comp.get("docs", solicitud_serie)
        cod_prov = comp.get("cod_prov", "")
        proveedor = comp.get("proveedor", "")

        if item in lm_padre and not comp.get("caso_15"):
            tipo = "PADRE COMPLETO SOLICITUD"
            datos_sap.append({
                "_item_origen": comp.get("item_origen", item),
                "_cantidad_origen": comp.get("cantidad_origen", plan),
                "PF_SERIE": serie,
                "PF_LINEA": lineNums,
                "PF_COD_PROV": cod_prov,
                "PF_PROVEEDOR": proveedor,
                "PF_SOL": docs,
                "PF_COMP": item,
                "PF_PADRE": item,
                "PF_CANT": float(plan),
                "PF_COM": float(plan),
                "PF_INC": 0.0,
                "PF_TOT": float(plan),
                "PF_CFAL": "",
                "PF_CAFAL": "",
                "PF_TIPO": tipo,
                "PF_CCOMPRA": "NO",
                "PF_PCOMPRA": compra_padre(item)
            })

        elif es_articulo_compra_sin_lm(item) and not comp.get("caso_15"):
            tipo = "ARTICULO DE COMPRA"
            datos_sap.append({
                "_item_origen": comp.get("item_origen", item),
                "_cantidad_origen": comp.get("cantidad_origen", plan),
                "PF_SERIE": serie,
                "PF_LINEA": lineNums,
                "PF_COD_PROV": cod_prov,
                "PF_PROVEEDOR": proveedor,
                "PF_SOL": docs,
                "PF_COMP": item,
                "PF_PADRE": item,
                "PF_CANT": float(plan),
                "PF_COM": 0.0,
                "PF_INC": 0.0,
                "PF_TOT": float(plan),
                "PF_CFAL": "",
                "PF_CAFAL": "",
                "PF_TIPO": tipo,
                "PF_CCOMPRA": "SI",
                "PF_PCOMPRA": ("SI" if comp.get("caso_15") else U(props_padre.get(item, {}).get("compra", "NO")))
            })

        else:
            tipo = (OBS_CASO_15 if comp.get("caso_15") else "COMPONENTE SUELTO")
            datos_sap.append({
                "_item_origen": comp.get("item_origen", item),
                "_cantidad_origen": comp.get("cantidad_origen", plan),
                "PF_SERIE": serie,
                "PF_LINEA": lineNums,
                "PF_COD_PROV": cod_prov,
                "PF_PROVEEDOR": proveedor,
                "PF_SOL": docs,
                "PF_COMP": item,
                "PF_PADRE": item,
                "PF_CANT": float(plan),
                "PF_COM": 0.0,
                "PF_INC": 0.0,
                "PF_TOT": float(plan),
                "PF_CFAL": "",
                "PF_CAFAL": "",
                "PF_TIPO": tipo,
                "PF_CCOMPRA": U(compra_por_item.get(item, "NO")),
                "PF_PCOMPRA": ("SI" if comp.get("caso_15") else U(props_padre.get(item, {}).get("compra", "NO")))
            })

def completar_consumo_fila(fila):
    """Asigna lo usado de la solicitud también en filas sin armado de LM.

    Se ejecuta después del armado; no cambia sus cantidades ni decisiones.
    El saldo por origen ya excluye todo lo utilizado por completas/incompletas.
    Los ceros identifican un detalle sin uso adicional, nunca un segundo consumo.
    """
    item = fila.pop("_item_origen", None) or fila["PF_PADRE"]
    cantidad_objetivo = D(fila.pop("_cantidad_origen", fila["PF_TOT"]))
    if cantidad_objetivo < 0:
        raise ValueError(f"Cantidad negativa en detalle: {item}")
    # Solo solicitudes y líneas asociadas a la fila original, nunca otras.
    docs = separar_valores(fila.get("PF_SOL", ""))
    grupos_lineas = S(fila.get("PF_LINEA", "")).split(" / ")
    lineas_por_doc = {}
    if len(docs) == len(grupos_lineas):
        for doc, grupo in zip(docs, grupos_lineas):
            lineas_por_doc.setdefault(doc, set()).update(
                x.strip() for x in grupo.split(",") if x.strip()
            )
    elif len(docs) == 1:
        lineas_por_doc[docs[0]] = {
            x.strip() for grupo in grupos_lineas for x in grupo.split(",") if x.strip()
        }
    else:
        raise ValueError(f"Solicitud y línea no alineadas: {fila.get('PF_SOL')}")
    candidatos = [
        lote for lote in origenes_stock.get(item, [])
        if lote["serie"] == fila["PF_SERIE"]
        and lote["solicitud"] in lineas_por_doc
        and (not lineas_por_doc[lote["solicitud"]]
             or lote["linea"] in lineas_por_doc[lote["solicitud"]])
    ]
    if not candidatos:
        raise ValueError(
            f"Sin origen de solicitud para {item}, serie {fila['PF_SERIE']}, "
            f"solicitudes {fila.get('PF_SOL')}, líneas {fila.get('PF_LINEA')}"
        )
    eventos = []
    pendiente = cantidad_objetivo
    for lote in candidatos:
        tomar = min(lote["saldo"], pendiente)
        if tomar > 0:
            lote["saldo"] -= tomar
            pendiente -= tomar
            eventos.append({
                "serie": lote["serie"], "solicitud": lote["solicitud"],
                "linea": lote["linea"], "item": item, "cantidad": tomar
            })
        if pendiente <= 0:
            break
    if eventos:
        fila.update(campos_consumo(eventos, fila["PF_SERIE"]))
    else:
        # Ya se asignó el origen a otra fila (por ejemplo, desglose informativo).
        # Se identifica el artículo real de la solicitud con uso adicional cero.
        fila["PF_SOL"] = " / ".join(x["solicitud"] for x in candidatos)
        fila["PF_LINEA"] = " / ".join(x["linea"] for x in candidatos)
        fila["PF_COMCONS"] = " / ".join(item for x in candidatos)
        fila["PF_CCON"] = " / ".join("0" for x in candidatos)
    fila["PF_USR"] = USUARIO


for fila in datos_sap:
    if fila["PF_TIPO"] not in ("SOLO COMPLETAS", "INCOMPLETA"):
        completar_consumo_fila(fila)
    else:
        # El armado ya registró el descuento exacto de cada origen.
        fila.setdefault("PF_COMCONS", "")
        fila.setdefault("PF_CCON", "")
        fila["PF_USR"] = USUARIO

# Pantalla y SAP respetan la prioridad de categoría del cálculo.
# Dentro de cada categoría: completas, incompletas y otros.
def clave_fila(tipo, item):
    if tipo in ("SOLO COMPLETAS", "PADRE COMPLETO SOLICITUD"):
        fase = 0
    elif tipo == "INCOMPLETA":
        fase = 1
    else:
        fase = 2
    categoria = clave_categoria(item)
    return (categoria[:2], fase, categoria[2])


datos_sap.sort(key=lambda fila: clave_fila(fila["PF_TIPO"], fila["PF_PADRE"]))
filas_ui = sorted(
    tr.get_children(""),
    key=lambda iid: clave_fila(tr.set(iid, "Tipo"), tr.set(iid, "Padre"))
)
for posicion, iid in enumerate(filas_ui):
    tr.move(iid, "", posicion)

# =====================================================
# ENVIAR A SAP 
# ================================================
Sub_SAP.insertar_preoferta_sillas(datos_sap, usuario=USUARIO)
    
#print("Filas preparadas para SAP:", len(datos_sap), flush=True)
#root.mainloop()


