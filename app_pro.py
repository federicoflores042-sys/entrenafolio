import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
from datetime import datetime
import io
from iol_client import IOLClient
import pyxirr
import db

# --- 1. CONFIGURACIÓN DE PÁGINA ---
st.set_page_config(page_title="Entrenanfolio Pro", layout="wide", page_icon="📈")

# --- 2. CONEXIÓN IOL ---
try:
    iol = IOLClient(st.secrets["IOL_USER"], st.secrets["IOL_PASS"])
except Exception as e:
    iol = None

# =============================================================================
# FUNCIONES DE CÁLCULO
# =============================================================================

def calcular_tir_cartera(df_movimientos, valor_actual_total):
    """
    Calcula TIR anualizada (XIRR).
    Requiere al menos un flujo negativo (inversión) y el valor actual positivo.
    Devuelve 0.0 si los datos son insuficientes o el resultado es absurdo (>500%).
    """
    try:
        if df_movimientos.empty or valor_actual_total <= 0:
            return 0.0

        # Convención XIRR estándar:
        # INGRESO (dinero que ponés) → negativo; EGRESO (retiro) → positivo
        # signo_flujo ya maneja esto; acá usamos directo sin invertir
        fechas = pd.to_datetime(df_movimientos['fecha']).tolist()
        flujos = df_movimientos['monto_calculado'].tolist()

        # Valor actual = flujo final positivo (lo que recuperarías hoy)
        fechas.append(pd.Timestamp.now())
        flujos.append(float(abs(valor_actual_total)))

        tiene_neg = any(f < 0 for f in flujos)
        tiene_pos = any(f > 0 for f in flujos)
        if not tiene_neg or not tiene_pos:
            return 0.0

        resultado = pyxirr.xirr(fechas, flujos)

        if resultado is None or abs(resultado) > 20.0:  # > 2000% = absurdo
            return 0.0

        return resultado
    except Exception:
        return 0.0


# =============================================================================
# FUNCIONES DE BASE DE DATOS
# =============================================================================

def validar_login(usuario, password):
    u_limpio = usuario.strip().lower()
    resultado = db.fetchone(
        "SELECT id_usuario, usuario FROM usuarios WHERE LOWER(usuario) = %s AND password = %s",
        (u_limpio, password.strip())
    )
    return resultado if resultado else None


def registrar_usuario(usuario, password):
    u_limpio = usuario.strip().lower()
    try:
        db.execute(
            "INSERT INTO usuarios (usuario, password) VALUES (%s, %s)",
            (u_limpio, password.strip())
        )
        exito = True
    except Exception:
        exito = False
    return exito


@st.cache_data(ttl=60)
def load_data_sqlite(user_id):
    """
    Carga SOLO posiciones ABIERTAS (cantidad neta > 0).
    Dividendos / Rentas / Amortizaciones NO se suman a la cantidad;
    se guardan aparte en Total_Rentas para el cálculo de rendimiento.
    """
    query = """
        WITH posiciones_abiertas AS (
            -- Primero identificamos qué tickers tienen posición neta abierta
            SELECT ticker, id_cartera
            FROM movimientos
            WHERE id_usuario = %(uid)s
              AND tipo_operacion IN ('COMPRA','VENTA','INGRESO','EGRESO')
            GROUP BY ticker, id_cartera
            HAVING SUM(cantidad) > 0.0001
        )
        SELECT
            m.ticker        AS "Ticker",
            MAX(m.moneda)   AS "Moneda",
            SUM(CASE WHEN m.tipo_operacion IN ('COMPRA','VENTA','INGRESO','EGRESO')
                     THEN m.cantidad ELSE 0 END)                        AS "Cantidad",
            -- Total_Rentas en USD: solo para posiciones abiertas
            SUM(CASE WHEN m.tipo_operacion IN ('RENTA','AMORTIZACIÓN')
                     THEN m.cantidad * ABS(COALESCE(m.precio_unitario, 1)) /
                          CASE WHEN m.moneda = 'ARS'
                               THEN COALESCE(m.ccl_al_dia, 1)
                               ELSE 1 END
                     ELSE 0 END)                                        AS "Total_Rentas",
            m.id_cartera    AS "Cartera",
            mt.ratio        AS "Ratio",
            mt.ticker_yahoo AS "Ticker_Yahoo",
            mt.activo       AS "Activo",
            AVG(CASE WHEN m.tipo_operacion = 'COMPRA'
                     THEN ABS(m.precio_unitario) END)                   AS "Costo_Unit_Compra"
        FROM movimientos m
        LEFT JOIN master_tickers mt ON m.ticker = mt.ticker
        -- Solo procesar tickers con posición abierta
        INNER JOIN posiciones_abiertas pa
            ON m.ticker = pa.ticker AND m.id_cartera = pa.id_cartera
        WHERE m.id_usuario = %(uid)s
        GROUP BY m.ticker, m.id_cartera, mt.ratio, mt.ticker_yahoo, mt.activo
        HAVING SUM(CASE WHEN m.tipo_operacion IN ('COMPRA','VENTA','INGRESO','EGRESO')
                     THEN m.cantidad ELSE 0 END) > 0.0001
    """
    df = db.run_query(query, {"uid": user_id})
    if not df.empty:
        df['Activo'] = df['Activo'].fillna('Varios')
        df['Total_Rentas'] = df['Total_Rentas'].fillna(0)
    return df


@st.cache_data(ttl=60)
def load_posiciones_cerradas(user_id):
    """
    Construye el cuadro de posiciones cerradas a partir de los movimientos de VENTA.
    Para renta fija suma RENTA y AMORTIZACIÓN recibidas al resultado.
    """
    # Compras: total cantidad comprada por ticker (para prorratear rentas)
    df_compras_total = db.run_query("""
        SELECT
            m.ticker,
            MAX(mt.activo)                                 AS activo,
            AVG(ABS(m.precio_unitario))                    AS precio_compra_prom,
            SUM(ABS(m.cantidad))                           AS cantidad_total_comprada,
            MAX(m.moneda)                                   AS moneda_compra,
            AVG(COALESCE(m.ccl_al_dia, 1))                 AS ccl_compra,
            MAX(mt.ratio)                                   AS ratio
        FROM movimientos m
        LEFT JOIN master_tickers mt ON m.ticker = mt.ticker
        WHERE m.id_usuario = %(uid)s AND m.tipo_operacion = 'COMPRA'
        GROUP BY m.ticker
    """, {"uid": user_id})

    # Ventas
    df_ventas = db.run_query("""
        SELECT
            m.ticker,
            m.fecha,
            ABS(m.cantidad)                                AS cantidad_vendida,
            ABS(m.precio_unitario)                         AS precio_venta,
            m.moneda                                        AS moneda_venta,
            COALESCE(m.ccl_al_dia, 1)                      AS ccl_venta,
            m.id_cartera                                    AS cartera,
            mt.activo,
            mt.ratio
        FROM movimientos m
        LEFT JOIN master_tickers mt ON m.ticker = mt.ticker
        WHERE m.id_usuario = %(uid)s AND m.tipo_operacion = 'VENTA'
    """, {"uid": user_id})

    # Rentas y Amortizaciones por ticker (para renta fija)
    df_rentas = db.run_query("""
        SELECT
            m.ticker,
            SUM(m.cantidad * COALESCE(m.precio_unitario, 1) /
                CASE WHEN m.moneda = 'ARS' THEN COALESCE(m.ccl_al_dia, 1) ELSE 1 END
            ) AS total_rentas_usd
        FROM movimientos m
        WHERE m.id_usuario = %(uid)s
          AND m.tipo_operacion IN ('RENTA', 'AMORTIZACIÓN')
        GROUP BY m.ticker
    """, {"uid": user_id})

    # Posiciones cerradas manuales (tabla opcional)
    try:
        df_manuales = db.run_query(
            "SELECT * FROM posiciones_cerradas WHERE id_usuario = %(uid)s",
            {"uid": user_id}
        )
    except Exception:
        df_manuales = pd.DataFrame()

    if df_ventas.empty and df_manuales.empty:
        return pd.DataFrame()

    filas = []
    if not df_ventas.empty and not df_compras_total.empty:
        df_v = df_ventas.merge(df_compras_total, on='ticker', how='left', suffixes=('', '_c'))
        df_v = df_v.merge(df_rentas, on='ticker', how='left')
        df_v['total_rentas_usd'] = df_v['total_rentas_usd'].fillna(0)
        df_v['cantidad_total_comprada'] = df_v['cantidad_total_comprada'].fillna(1).replace(0, 1)

        for _, r in df_v.iterrows():
            ratio = r['ratio'] if pd.notna(r['ratio']) and r['ratio'] > 0 else 1
            activo = r['activo'] if pd.notna(r['activo']) else 'Varios'

            # Precio venta en USD
            pv = r['precio_venta']
            if r['moneda_venta'] == 'ARS':
                pv = pv / r['ccl_venta']
            if activo == 'Cedears':
                pv = pv / ratio

            # Precio compra en USD
            pc = r.get('precio_compra_prom', 0) or 0
            if r.get('moneda_compra') == 'ARS':
                pc = pc / (r.get('ccl_compra') or 1)
            if activo == 'Cedears':
                pc = pc / ratio

            ganancia_capital = (pv - pc) * r['cantidad_vendida']

            # Prorratear rentas/dividendos para cualquier tipo de activo
            ganancia_renta = 0.0
            if r['total_rentas_usd'] > 0:
                proporcion = r['cantidad_vendida'] / r['cantidad_total_comprada']
                proporcion = min(proporcion, 1.0)
                ganancia_renta = r['total_rentas_usd'] * proporcion

            ganancia_total = ganancia_capital + ganancia_renta
            roi = (ganancia_total / (pc * r['cantidad_vendida'])) * 100 if pc > 0 else 0

            filas.append({
                'Fecha': r['fecha'],
                'Ticker': r['ticker'],
                'Tipo': activo,
                'Cartera': r['cartera'],
                'Cantidad': r['cantidad_vendida'],
                'P. Compra (USD)': round(pc, 4),
                'P. Venta (USD)': round(pv, 4),
                'Gan. Capital (USD)': round(ganancia_capital, 2),
                'Gan. Renta (USD)': round(ganancia_renta, 2),
                'Ganancia Total (USD)': round(ganancia_total, 2),
                'ROI %': round(roi, 2),
                'Origen': 'Automático',
            })

    if not df_manuales.empty:
        for _, r in df_manuales.iterrows():
            filas.append({
                'Fecha': r.get('fecha', ''),
                'Ticker': r.get('ticker', ''),
                'Tipo': r.get('activo', ''),
                'Cartera': r.get('cartera', ''),
                'Cantidad': r.get('cantidad', 0),
                'P. Compra (USD)': r.get('precio_compra', 0),
                'P. Venta (USD)': r.get('precio_venta', 0),
                'Gan. Capital (USD)': r.get('ganancia_capital', 0),
                'Gan. Renta (USD)': r.get('ganancia_renta', 0),
                'Ganancia Total (USD)': r.get('ganancia_total', 0),
                'ROI %': r.get('roi', 0),
                'Origen': 'Manual',
            })

    return pd.DataFrame(filas)


def init_posiciones_cerradas_table():
    """Crea la tabla de posiciones cerradas manuales si no existe."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS posiciones_cerradas (
            id SERIAL PRIMARY KEY,
            id_usuario INTEGER,
            fecha TEXT,
            ticker TEXT,
            activo TEXT,
            cartera TEXT,
            cantidad REAL,
            precio_compra REAL,
            precio_venta REAL,
            moneda TEXT,
            ganancia_capital REAL,
            ganancia_renta REAL,
            ganancia_total REAL,
            roi REAL
        )
    """)


def migrar_cash_multimoneda():
    """
    Migración idempotente: separa el ticker genérico 'CASH' en 'CASH_ARS' /
    'CASH_USD' según la moneda de cada movimiento, para no mezclar pesos y
    dólares en una misma suma. Se puede ejecutar en cada arranque sin riesgo:
    una vez migrados, ya no quedan filas con ticker='CASH' para volver a tocar.
    """
    db.execute("""
        UPDATE movimientos
        SET ticker = 'CASH_' || moneda
        WHERE ticker = 'CASH' AND moneda IN ('ARS', 'USD')
    """)


# =============================================================================
# MOTOR DE PRECIOS — MEP y CCL separados
# =============================================================================

@st.cache_data(ttl=300)
def obtener_tipos_cambio():
    """
    Devuelve (mep, ccl).
    CCL  = GGAL.BA / GGAL  × 10
    MEP  = AL30.BA / AL30  × 100  (factor 100 por lámina del bono)
    """
    # --- CCL ---
    try:
        _ggal_ba = yf.download("GGAL.BA", period="2d", interval="1h", auto_adjust=True)['Close'].dropna()
        _ggal_us = yf.download("GGAL",    period="2d", interval="1h", auto_adjust=True)['Close'].dropna()
        ggal_ba = float(_ggal_ba.iloc[-1].item() if hasattr(_ggal_ba.iloc[-1], 'item') else _ggal_ba.iloc[-1])
        ggal_us = float(_ggal_us.iloc[-1].item() if hasattr(_ggal_us.iloc[-1], 'item') else _ggal_us.iloc[-1])
        ccl = round((ggal_ba * 10) / ggal_us, 2)
    except Exception:
        ccl = 1515.0

    # --- MEP ---
    # AL30.BA / AL30D (en USD) × factor 100 (lámina del bono)
    # Nota: "AL30" en Yahoo puede resolver un ticker europeo; usamos GD30 como respaldo
    mep = None
    for ticker_ba, ticker_us in [("AL30.BA", "AL30"), ("GD30.BA", "GD30")]:
        try:
            _ba = yf.download(ticker_ba, period="2d", interval="1h", auto_adjust=True)['Close'].dropna()
            _us = yf.download(ticker_us, period="2d", interval="1h", auto_adjust=True)['Close'].dropna()
            if _ba.empty or _us.empty:
                continue
            p_ba = float(_ba.iloc[-1].item() if hasattr(_ba.iloc[-1], 'item') else _ba.iloc[-1])
            p_us = float(_us.iloc[-1].item() if hasattr(_us.iloc[-1], 'item') else _us.iloc[-1])
            # Validar que el precio en USD sea coherente para un bono soberano AR (>30 y <150)
            if p_us < 30 or p_us > 150:
                continue
            mep = round(p_ba / p_us, 2)
            break
        except Exception:
            continue
    if mep is None or mep < 800 or mep > 5000:
        mep = ccl * 0.97

    return mep, ccl


@st.cache_data(ttl=600)
def obtener_precio_cached(ticker, ratio, activo, mep, ccl):
    """
    Devuelve precio en USD por unidad.
    CEDEARs: busca primero en BCBA (.BA) -> precio ARS / CCL. Sin depender de ratio/NYSE.
    """
    tc = ccl if activo == 'Cedears' else mep
    es_ticker_ba = ticker.strip().upper().endswith(".BA")
    ticker_base  = ticker.replace(".BA", "").strip().upper()

    def _precio_yahoo(tk):
        try:
            asset = yf.Ticker(tk)
            try:
                p = float(asset.fast_info['last_price'])
                if p and p > 0:
                    return p
            except Exception:
                pass
            hist = asset.history(period="5d")
            if not hist.empty:
                p = float(hist['Close'].dropna().iloc[-1])
                if p > 0:
                    return p
        except Exception:
            pass
        return None

    # 1. IOL (precio siempre en ARS)
    if iol:
        try:
            precio_iol = iol.obtener_precio(ticker_base)
            if precio_iol and precio_iol > 0:
                if activo == 'Cedears':
                    return float(precio_iol / ccl)
                if activo in ['Bonos', 'Titulos Publicos', 'Bono']:
                    return float(precio_iol / 100.0 / tc) if precio_iol > 5 else float(precio_iol / tc)
                return float(precio_iol / tc)
        except Exception:
            pass

    # 2. CEDEARs: siempre usar precio ARS en BCBA (ticker.BA) / CCL
    if activo == 'Cedears':
        precio_ba = _precio_yahoo(ticker_base + ".BA")
        if precio_ba and precio_ba > 10:
            return float(precio_ba / ccl)
        # Fallback NYSE: precio_usd / ratio
        precio_usd = _precio_yahoo(ticker_base)
        if precio_usd and precio_usd > 0:
            cedear_usd = precio_usd / ratio if ratio >= 1 else precio_usd * ratio
            if cedear_usd > 0.1:
                return float(cedear_usd)
        return 0.0

    # 3. Renta Fija
    if activo in ['Bonos', 'Titulos Publicos', 'Bono', 'Obligaciones Negociables', 'ON']:
        precio = _precio_yahoo(ticker)
        if precio is None:
            return 0.0
        if es_ticker_ba:
            if precio > 100:
                return precio / tc
            elif precio > 5:
                return precio / 100.0
            else:
                return precio
        else:
            return precio / 100.0 if precio > 5 else precio

    # 4. Acciones locales (.BA)
    if es_ticker_ba:
        precio = _precio_yahoo(ticker)
        return float(precio / mep) if precio else 0.0

    # 5. Acciones extranjeras en USD
    precio = _precio_yahoo(ticker)
    return float(precio) if precio else 0.0


# =============================================================================
# LÓGICA DE SESIÓN
# =============================================================================
if 'user_id' not in st.session_state:
    st.session_state.user_id = None

init_posiciones_cerradas_table()
migrar_cash_multimoneda()

# =============================================================================
# PANTALLA DE LOGIN
# =============================================================================
if st.session_state.user_id is None:
    st.title("🚀 Bienvenido a Entrenanfolio")
    tab_login, tab_registro = st.tabs(["Ingresar", "Registrarme"])

    with tab_login:
        with st.form("Login"):
            u_input = st.text_input("Usuario")
            p_input = st.text_input("Contraseña", type="password")
            if st.form_submit_button("Ingresar", use_container_width=True):
                res = validar_login(u_input, p_input)
                if res:
                    st.session_state.user_id = res[0]
                    st.session_state.user_name = res[1]
                    st.rerun()
                else:
                    st.error("Usuario o contraseña incorrectos.")

    with tab_registro:
        with st.form("Registro"):
            st.subheader("Crea tu cuenta")
            new_u  = st.text_input("Nuevo Usuario")
            new_p  = st.text_input("Nueva Contraseña", type="password")
            conf_p = st.text_input("Confirmar Contraseña", type="password")
            if st.form_submit_button("Crear Cuenta", use_container_width=True):
                if new_p != conf_p:
                    st.error("Las contraseñas no coinciden.")
                elif len(new_u) < 3:
                    st.error("El usuario debe tener al menos 3 caracteres.")
                else:
                    if registrar_usuario(new_u, new_p):
                        st.success("✅ ¡Cuenta creada! Ya podés ingresar.")
                    else:
                        st.error("⚠️ El nombre de usuario ya está en uso.")

# =============================================================================
# DASHBOARD LOGUEADO
# =============================================================================
else:
    # ── Tipos de cambio ────────────────────────────────────────────────────────
    tc_mep, tc_ccl = obtener_tipos_cambio()

    # ── Sidebar ────────────────────────────────────────────────────────────────
    st.sidebar.title(f"👤 {st.session_state.user_name}")
    st.sidebar.write("")
    
    moneda_visualizacion = st.sidebar.radio("Ver en:", ["USD", "ARS"], horizontal=True)
    if st.sidebar.button("Cerrar Sesión"):
        st.session_state.user_id = None
        st.rerun()

    es_admin = st.session_state.user_name.lower() == "federicoflores"

    # ── Carga de datos ─────────────────────────────────────────────────────────
    df = load_data_sqlite(st.session_state.user_id)

    # ── Procesamiento de precios ───────────────────────────────────────────────
    if not df.empty:
        df_master_precios = db.run_query(
            "SELECT ticker, ultimo_precio FROM master_tickers"
        )
        dict_precios_manuales = dict(
            zip(df_master_precios['ticker'], df_master_precios['ultimo_precio'])
        )

        precios_usd = {}   # siempre en USD

        with st.spinner('Actualizando cotizaciones...'):
            for _, row in df.iterrows():
                ticker  = row['Ticker']
                activo  = row['Activo']
                ratio   = row['Ratio'] if pd.notna(row['Ratio']) and row['Ratio'] > 0 else 1
                moneda  = row['Moneda']

                # Precio manual del Master
                p_manual = dict_precios_manuales.get(ticker, 0) or 0
                if p_manual > 0:
                    tc_ref = tc_ccl if activo == 'Cedears' else tc_mep
                    precios_usd[ticker] = p_manual / tc_ref if (moneda == 'ARS' and p_manual > 500) else p_manual
                    continue

                # CASH (separado por moneda: CASH_ARS / CASH_USD)
                if ticker.startswith('CASH'):
                    precios_usd[ticker] = 1.0 if moneda == 'USD' else (1.0 / tc_mep)
                    continue

                # Activos de mercado
                tk_search = (
                    row['Ticker_Yahoo']
                    if pd.notna(row['Ticker_Yahoo']) and row['Ticker_Yahoo'] != ''
                    else ticker
                )
                precios_usd[ticker] = obtener_precio_cached(
                    tk_search, ratio, activo, tc_mep, tc_ccl
                )

        # ── Columnas de precio y costo en USD ──────────────────────────────────
        def precio_usd_unitario(row):
            if row['Ticker'].startswith('CASH'):
                return 1.0 if row['Moneda'] == 'USD' else 1.0 / tc_mep
            return precios_usd.get(row['Ticker'], 0.0)

        def costo_usd_unitario(row):
            if row['Ticker'].startswith('CASH'):
                return 1.0 if row['Moneda'] == 'USD' else 1.0 / tc_mep
            costo  = row['Costo_Unit_Compra'] or 0
            activo = row['Activo']
            ratio  = row['Ratio'] if pd.notna(row['Ratio']) and row['Ratio'] > 0 else 1
            tc_ref = tc_ccl if activo == 'Cedears' else tc_mep
            if row['Moneda'] == 'ARS':
                # Precio guardado en ARS → ya es precio del CEDEAR (no del subyacente)
                # Solo dividir por CCL para convertir a USD. NUNCA dividir por ratio.
                return costo / tc_ref
            if activo == 'Cedears':
                # Precio guardado en USD → es precio de la acción subyacente (NYSE)
                # Dividir por ratio para obtener el precio por CEDEAR
                return costo / ratio if ratio >= 1 else costo * ratio
            return costo

        df['Precio_USD'] = df.apply(precio_usd_unitario, axis=1)
        df['Costo_USD']  = df.apply(costo_usd_unitario,  axis=1)

        # ── Factor de visualización ────────────────────────────────────────────
        # Para ARS: acciones/bonos → MEP, CEDEARs → CCL
        def mult_row(row):
            if moneda_visualizacion == 'USD':
                return 1.0
            return tc_ccl if row['Activo'] == 'Cedears' else tc_mep

        df['mult'] = df.apply(mult_row, axis=1)

        simbolo = "$" if moneda_visualizacion == "ARS" else "USD"
        simbolo_html = "&#36;" if moneda_visualizacion == "ARS" else "USD"

        df['Precio_V']         = df['Precio_USD'] * df['mult']
        df['Costo_V']          = df['Costo_USD']  * df['mult']
        df['Valuacion_V']      = df['Precio_V']   * df['Cantidad']
        df['Inversion_Total_V']= df['Costo_V']    * df['Cantidad']

        df['Ganancia_Capital'] = df['Valuacion_V'] - df['Inversion_Total_V']
        # Total_Rentas ya está normalizado a USD en la query → convertir a moneda de visualización
        df['Ganancia_Renta']   = df['Total_Rentas'].fillna(0) * df['mult']
        df['Ganancia_Nominal'] = df['Ganancia_Capital'] + df['Ganancia_Renta']
        df['ROI_%'] = (
            df['Ganancia_Nominal'] /
            df['Inversion_Total_V'].replace(0, 0.00001)
        ) * 100
        df['ROI_%'] = df['ROI_%'].fillna(0).replace([float('inf'), float('-inf')], 0)

        # ── Métricas globales ──────────────────────────────────────────────────
        st.title(f"📊 Mi Portafolio — {st.session_state.user_name}")

        total_patrimonio = float(df['Valuacion_V'].sum())
        total_ganancia   = float(df['Ganancia_Nominal'].sum())

        # Ganancia / Pérdida TOTAL: posiciones abiertas + posiciones cerradas
        df_cerradas_resumen = load_posiciones_cerradas(st.session_state.user_id)
        gan_cerradas_usd = float(df_cerradas_resumen['Ganancia Total (USD)'].sum()) if not df_cerradas_resumen.empty else 0.0
        # Convertir ganancia de cerradas a moneda de visualización
        gan_cerradas_v = gan_cerradas_usd * (tc_mep if moneda_visualizacion == 'ARS' else 1.0)
        total_ganancia_completa = total_ganancia + gan_cerradas_v

        def _card(label, value, delta=None, value2=None, big=False):
            """Kept for backwards compat but no longer used for main cards."""
            pass

        # Patrimonio siempre en USD y ARS MEP
        patrimonio_usd = float(df['Valuacion_V'].sum()) if moneda_visualizacion == 'USD' else float((df['Valuacion_V'] / df['mult']).sum())
        patrimonio_ars = patrimonio_usd * tc_mep

        # ── Cards principales con st.metric (sin markdown → sin bug LaTeX) ──
        c1, c2, c3, c4, c5 = st.columns(5)

        patrimonio_label = f"USD {patrimonio_usd:,.2f}"
        patrimonio_sub   = f"ARS {patrimonio_ars:,.0f}"

        gan_signo  = "ARS" if moneda_visualizacion == "ARS" else "USD"
        gan_label  = f"{gan_signo} {total_ganancia_completa:,.2f}"

        with c1:
            st.components.v1.html(f"""
<div style="background:#1e1e2e;border-radius:8px;padding:12px 16px;font-family:sans-serif;height:100px;box-sizing:border-box">
  <div style="font-size:11px;color:#aaa;margin-bottom:4px">Patrimonio Total</div>
  <div style="font-size:16px;font-weight:700;color:#fff;line-height:1.3">{patrimonio_label}</div>
  <div style="font-size:16px;font-weight:700;color:#fff;line-height:1.3">{patrimonio_sub}</div>
</div>""", height=110)

        gan_color = "#2ecc71" if total_ganancia_completa >= 0 else "#e74c3c"
        gan_arrow = "▲" if total_ganancia_completa >= 0 else "▼"
        with c2:
            st.components.v1.html(f"""
<div style="background:#1e1e2e;border-radius:8px;padding:12px 16px;font-family:sans-serif;height:100px;box-sizing:border-box">
  <div style="font-size:11px;color:#aaa;margin-bottom:4px">Ganancia / Perdida Total</div>
  <div style="font-size:16px;font-weight:700;color:{gan_color}">{gan_signo} {total_ganancia_completa:,.2f}</div>
  <div style="font-size:11px;color:#aaa">(abiertas + cerradas)</div>
  <div style="font-size:12px;color:{gan_color}">{gan_arrow} {abs(total_ganancia_completa):,.2f}</div>
</div>""", height=110)

        tir_placeholder = c3.empty()
        tir_placeholder.html(f"""
<div style="background:#1e1e2e;border-radius:8px;padding:12px 16px;font-family:sans-serif;height:100px;box-sizing:border-box">
  <div style="font-size:11px;color:#aaa;margin-bottom:4px">TIR CARTERA</div>
  <div style="font-size:18px;font-weight:700;color:#fff">—</div>
  <div style="font-size:11px;color:#aaa">calculando...</div>
</div>""")

        with c4:
            st.components.v1.html(f"""
<div style="background:#1e1e2e;border-radius:8px;padding:12px 16px;font-family:sans-serif;height:100px;box-sizing:border-box;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center">
  <div style="font-size:11px;color:#aaa;margin-bottom:6px">Dolar MEP</div>
  <div style="font-size:16px;font-weight:700;color:#fff">ARS {tc_mep:,.2f}</div>
</div>""", height=110)

        with c5:
            st.components.v1.html(f"""
<div style="background:#1e1e2e;border-radius:8px;padding:12px 16px;font-family:sans-serif;height:100px;box-sizing:border-box;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center">
  <div style="font-size:11px;color:#aaa;margin-bottom:6px">Dolar CCL</div>
  <div style="font-size:16px;font-weight:700;color:#fff">ARS {tc_ccl:,.2f}</div>
</div>""", height=110)


        # TIR — todos los flujos normalizados a USD usando el TC histórico de cada movimiento
        df_mov_tir_full = db.run_query("""
            SELECT
                m.fecha,
                m.tipo_operacion,
                -- Normalización a USD usando el TC histórico del día del movimiento
                -- Si moneda=ARS → dividir por ccl_al_dia (o CCL actual como fallback)
                -- Si moneda=USD → ya está en USD
                (m.cantidad * ABS(COALESCE(m.precio_unitario, 1)) /
                    CASE WHEN m.moneda='ARS'
                         THEN COALESCE(m.ccl_al_dia, %(tc_ccl)s)
                         ELSE 1 END /
                    CASE WHEN mt.activo='Cedears' AND COALESCE(mt.ratio,0)>0
                         THEN mt.ratio ELSE 1 END
                ) AS monto_usd
            FROM movimientos m
            LEFT JOIN master_tickers mt ON m.ticker = mt.ticker
            WHERE m.id_usuario = %(uid)s
              AND m.tipo_operacion IN ('INGRESO','EGRESO','COMPRA','VENTA')
        """, {"tc_ccl": tc_ccl, "uid": st.session_state.user_id})

        if not df_mov_tir_full.empty:
            def signo_flujo(row):
                op = row['tipo_operacion']
                monto = abs(row['monto_usd'])
                # Convención XIRR estándar (siempre igual, con o sin INGRESO):
                # INGRESO = dinero que ponés en la cartera         → negativo
                # EGRESO  = dinero que retirás de la cartera       → positivo
                # COMPRA  = inversión en activo                    → negativo
                # VENTA   = recupero de inversión                  → positivo
                if op == 'INGRESO': return -monto
                if op == 'EGRESO':  return  monto
                if op == 'COMPRA':  return -monto
                if op == 'VENTA':   return  monto
                return 0.0

            df_mov_tir_full['flujo'] = df_mov_tir_full.apply(signo_flujo, axis=1)
            df_mov_tir = df_mov_tir_full[df_mov_tir_full['flujo'] != 0][['fecha','flujo']].rename(
                columns={'flujo': 'monto_calculado'}
            )
            # TIR Total: activos + CASH (toda la plata ingresada al sistema)
            # TIR Activos: solo instrumentos, sin penalizar por liquidez ociosa
            df_activos = df[~df['Ticker'].str.startswith('CASH')]

            if moneda_visualizacion == 'USD':
                patrimonio_total_usd   = float(df['Valuacion_V'].sum())
                patrimonio_activos_usd = float(df_activos['Valuacion_V'].sum())
            else:
                patrimonio_total_usd   = float((df['Valuacion_V'] / df['mult']).sum())
                patrimonio_activos_usd = float((df_activos['Valuacion_V'] / df_activos['mult']).sum())

            tir_total   = calcular_tir_cartera(df_mov_tir, patrimonio_total_usd)
            tir_activos = calcular_tir_cartera(df_mov_tir, patrimonio_activos_usd)
        else:
            tir_total   = 0.0
            tir_activos = 0.0

        st.sidebar.markdown("**TIR Anualizada**")
        col_tir = st.sidebar.container()
        col_tir.markdown(
            f"- Cartera total (c/liquidez): **{tir_total:.2%}**\n"
            f"- Solo activos invertidos: **{tir_activos:.2%}**"
        )

        # Actualizar el card de TIR con el valor real calculado
        tir_display = f"{tir_total:.2%}" if tir_total != 0.0 else "N/D"
        tir_color = "#2ecc71" if tir_total >= 0 else "#e74c3c"
        tir_placeholder.html(f"""
<div style="background:#1e1e2e;border-radius:8px;padding:12px 16px;font-family:sans-serif;height:100px;box-sizing:border-box">
  <div style="font-size:11px;color:#aaa;margin-bottom:4px">TIR CARTERA</div>
  <div style="font-size:18px;font-weight:700;color:{tir_color}">{tir_display}</div>
  <div style="font-size:11px;color:#aaa">c/posiciones cerradas y liquidez</div>
</div>""")

    # ── Pestañas ───────────────────────────────────────────────────────────────
    titulos_tabs = ["💰 Portafolio", "📋 Posic. Cerradas", "➕ Nueva Operación", "🎯 Metas"]
    if es_admin:
        titulos_tabs.append("🛠️ Admin Master")

    tabs      = st.tabs(titulos_tabs)
    tab_port  = tabs[0]
    tab_cerr  = tabs[1]
    tab_op    = tabs[2]
    tab_metas = tabs[3]
    if es_admin:
        tab_admin = tabs[4]

    # ==========================================================================
    # TAB 1 — PORTAFOLIO
    # ==========================================================================
    with tab_port:
        if df.empty:
            st.info("No hay posiciones abiertas. Registrá tu primera operación.")
        else:
            # ── CUADRO DE LIQUIDEZ ─────────────────────────────────────────────
            df_cash = df[df['Ticker'].str.startswith('CASH')].copy()
            st.subheader("💵 Liquidez")
            if not df_cash.empty:
                # Separar correctamente ARS y USD para evitar doble conteo
                df_cash_ars = df_cash[df_cash['Moneda'] == 'ARS']
                df_cash_usd = df_cash[df_cash['Moneda'] == 'USD']

                total_ars     = float(df_cash_ars['Cantidad'].sum()) if not df_cash_ars.empty else 0.0
                usd_directo   = float(df_cash_usd['Cantidad'].sum()) if not df_cash_usd.empty else 0.0

                usd_mep_equiv = (total_ars / tc_mep) + usd_directo
                usd_ccl_equiv = (total_ars / tc_ccl) + usd_directo

                lc1, lc2, lc3, lc4 = st.columns(4)
                lc1.metric("Pesos (ARS)",        f"$ {total_ars:,.2f}")
                lc2.metric("USD directo",         f"USD {usd_directo:,.2f}")
                lc3.metric("Equiv. MEP",   f"USD {usd_mep_equiv:,.2f}")
                lc4.metric("Equiv. CCL",   f"USD {usd_ccl_equiv:,.2f}")
            else:
                st.info("Sin efectivo registrado.")

            st.markdown("---")

            # ── HELPER para subtotal ───────────────────────────────────────────
            def subtotal_bloque(df_bloque, label):
                if df_bloque.empty:
                    return
                total_val = df_bloque['Valuacion_V'].sum()
                total_gan = df_bloque['Ganancia_Nominal'].sum()
                pct_total = (total_val / total_patrimonio * 100) if total_patrimonio else 0
                st.markdown(
                    f"**{label}** — "
                    f"{simbolo} {total_val:,.2f} | "
                    f"Ganancia: {simbolo} {total_gan:,.2f} | "
                    f"% Cartera: {pct_total:.1f}%"
                )

            def tabla_posiciones(df_bloque):
                if df_bloque.empty:
                    st.caption("Sin posiciones en este instrumento.")
                    return
                df_d = df_bloque[[
                    'Ticker', 'Cantidad', 'Activo', 'Costo_V',
                    'Precio_V', 'Valuacion_V', 'Ganancia_Nominal', 'ROI_%'
                ]].copy()

                # Formateamos manualmente para evitar el bug removeChild del Styler en expanders
                def fmt_cant(x):
                    if 0 < x < 1: return f"{x:,.6f}"
                    if x == int(x): return f"{int(x):,}"
                    return f"{x:,.2f}"

                def fmt_gan(x):
                    prefix = "▲ " if x > 0 else ("▼ " if x < 0 else "")
                    return f"{prefix}{x:,.2f}"

                def fmt_roi(x):
                    prefix = "▲ " if x > 0 else ("▼ " if x < 0 else "")
                    return f"{prefix}{x:,.2f}%"

                df_d['Cant.']    = df_d['Cantidad'].apply(fmt_cant)
                df_d['Ganancia'] = df_d['Ganancia_Nominal'].apply(fmt_gan)
                df_d['ROI %']    = df_d['ROI_%'].apply(fmt_roi)
                # Nombres de columna FIJOS (sin f-string con simbolo) → evita bug removeChild
                mon_label = moneda_visualizacion  # "USD" o "ARS"
                df_d[f'Costo ({mon_label})']  = df_d['Costo_V'].apply(lambda x: f"{x:,.2f}" if pd.notna(x) else "-")
                df_d[f'Precio ({mon_label})'] = df_d['Precio_V'].apply(lambda x: f"{x:,.2f}" if pd.notna(x) else "-")
                df_d[f'Total ({mon_label})']  = df_d['Valuacion_V'].apply(lambda x: f"{x:,.2f}")

                cols_show = [
                    'Ticker', 'Cant.', 'Activo',
                    f'Costo ({mon_label})', f'Precio ({mon_label})',
                    f'Total ({mon_label})', 'Ganancia', 'ROI %'
                ]
                st.dataframe(
                    df_d[cols_show],
                    use_container_width=True,
                    hide_index=True,
                )

            # ── Filtrado sin CASH ──────────────────────────────────────────────
            df_inv = df[~df['Ticker'].str.startswith('CASH')]

            # Clasificación
            ACTIVOS_RF_BONOS = ['Bonos', 'Títulos Públicos', 'Bono']
            ACTIVOS_RF_ON    = ['Obligaciones Negociables', 'ON']
            ACTIVOS_ACC      = ['Acciones', 'Acción']
            ACTIVOS_CED      = ['Cedears']

            df_bonos   = df_inv[df_inv['Activo'].isin(ACTIVOS_RF_BONOS)]
            df_ons     = df_inv[df_inv['Activo'].isin(ACTIVOS_RF_ON)]
            df_acc     = df_inv[df_inv['Activo'].isin(ACTIVOS_ACC)]
            df_ceds    = df_inv[df_inv['Activo'].isin(ACTIVOS_CED)]
            df_otros   = df_inv[~df_inv['Activo'].isin(
                ACTIVOS_RF_BONOS + ACTIVOS_RF_ON + ACTIVOS_ACC + ACTIVOS_CED
            )]

            df_rf = pd.concat([df_bonos, df_ons])
            df_rv = pd.concat([df_acc, df_ceds])

            # ── RENTA FIJA ─────────────────────────────────────────────────────
            st.subheader("📄 Renta Fija")
            subtotal_bloque(df_rf, "SUBTOTAL Renta Fija")

            with st.expander("📌 Títulos Públicos", expanded=True):
                subtotal_bloque(df_bonos, "Subtotal Títulos Públicos")
                tabla_posiciones(df_bonos)

            with st.expander("📌 Obligaciones Negociables", expanded=True):
                subtotal_bloque(df_ons, "Subtotal ONs")
                tabla_posiciones(df_ons)

            st.markdown("---")

            # ── RENTA VARIABLE ─────────────────────────────────────────────────
            st.subheader("📈 Renta Variable")
            subtotal_bloque(df_rv, "SUBTOTAL Renta Variable")

            with st.expander("📌 Acciones", expanded=True):
                subtotal_bloque(df_acc, "Subtotal Acciones")
                tabla_posiciones(df_acc)

            with st.expander("📌 CEDEARs  (tipo de cambio: CCL)", expanded=True):
                subtotal_bloque(df_ceds, "Subtotal CEDEARs")
                tabla_posiciones(df_ceds)

            if not df_otros.empty:
                with st.expander("📌 Otros instrumentos", expanded=False):
                    subtotal_bloque(df_otros, "Subtotal Otros")
                    tabla_posiciones(df_otros)

            st.markdown("---")

            # ── Gráfico torta ──────────────────────────────────────────────────
            fig = px.pie(
                df_inv,
                values='Valuacion_V',
                names='Ticker',
                hole=0.4,
                template="plotly_dark",
                title=f"Distribución de Cartera ({moneda_visualizacion})"
            )
            fig.update_traces(
                textposition='inside',
                textinfo='percent+label',
                hovertemplate=(
                    f"<b>%{{label}}</b><br>"
                    f"Total: {simbolo} %{{value:,.2f}}<br>"
                    f"Participación: %{{percent}}"
                )
            )
            st.plotly_chart(fig, use_container_width=True)

            # ── Exportar Excel ─────────────────────────────────────────────────
            buf = io.BytesIO()
            df_excel = df_inv[[
                'Ticker', 'Activo', 'Cantidad', 'Costo_V',
                'Precio_V', 'Valuacion_V', 'Ganancia_Nominal', 'ROI_%'
            ]].copy()
            df_excel.columns = [
                'Ticker', 'Tipo', 'Cantidad',
                f'Costo ({simbolo})', f'Precio ({simbolo})',
                f'Total ({simbolo})', 'Ganancia', 'ROI %'
            ]
            with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                df_excel.to_excel(writer, index=False, sheet_name='Portafolio')
                ws = writer.sheets['Portafolio']
                for i, col in enumerate(df_excel.columns):
                    col_len = max(df_excel[col].astype(str).str.len().max(), len(col)) + 2
                    ws.set_column(i, i, col_len)
            st.download_button(
                "📥 Descargar Portafolio (Excel)",
                data=buf,
                file_name=f"Portafolio_{moneda_visualizacion}.xlsx",
                mime="application/vnd.ms-excel",
                use_container_width=True,
            )

            # ── Libro Diario ───────────────────────────────────────────────────
            st.markdown("---")
            st.subheader("📝 Libro Diario de Movimientos")
            df_hist = db.run_query(
                """SELECT id_movimiento, fecha, ticker, tipo_operacion, cantidad,
                          precio_unitario, moneda, id_cartera
                   FROM movimientos
                   WHERE id_usuario = %(uid)s
                   ORDER BY fecha DESC""",
                {"uid": st.session_state.user_id}
            )

            if not df_hist.empty:
                OP_ICON = {
                    'INGRESO': '🟢', 'COMPRA': '🟢', 'RENTA': '🟢', 'AMORTIZACIÓN': '🟢',
                    'EGRESO': '🔴', 'VENTA': '🔴', 'SPLIT': '🔵',
                }
                df_hist_vis = df_hist.copy()
                df_hist_vis['tipo_operacion'] = df_hist_vis['tipo_operacion'].apply(
                    lambda x: f"{OP_ICON.get(x, '⚪')} {x}"
                )
                # Mostrar sin columna id_movimiento (se usa internamente)
                st.dataframe(
                    df_hist_vis.drop(columns=['id_movimiento']),
                    use_container_width=True,
                    hide_index=True
                )
                buf_h = io.BytesIO()
                with pd.ExcelWriter(buf_h, engine='xlsxwriter') as writer:
                    df_hist.drop(columns=['id_movimiento']).to_excel(writer, index=False, sheet_name='Movimientos')
                st.download_button(
                    "📥 Descargar Historial Completo (Excel)",
                    data=buf_h.getvalue(),
                    file_name=f"Historial_{st.session_state.user_name}.xlsx",
                    mime="application/vnd.ms-excel",
                    use_container_width=True,
                    key="btn_hist",
                )

                # ── Eliminar movimiento individual ─────────────────────────────
                st.markdown("---")
                with st.expander("🗑️ Eliminar movimiento individual"):
                    st.warning("⚠️ Esta acción es irreversible. Seleccioná el movimiento a eliminar.")

                    # Armar etiqueta legible para cada fila
                    df_hist['_label'] = df_hist.apply(
                        lambda r: f"{r['fecha']} | {r['ticker']} | {r['tipo_operacion']} | {r['cantidad']:,.2f} {r['moneda']} | Cartera: {r['id_cartera']}",
                        axis=1
                    )
                    opciones = {row['_label']: row['id_movimiento'] for _, row in df_hist.iterrows()}

                    sel_label = st.selectbox(
                        "Seleccioná el movimiento:",
                        options=list(opciones.keys()),
                        key="sel_mov_borrar"
                    )
                    sel_id = opciones[sel_label]

                    if st.button("🗑️ Eliminar este movimiento", type="primary", use_container_width=True, key="btn_borrar_mov"):
                        db.execute(
                            "DELETE FROM movimientos WHERE id_movimiento = %s AND id_usuario = %s",
                            (sel_id, st.session_state.user_id)
                        )
                        st.success("✅ Movimiento eliminado correctamente.")
                        st.cache_data.clear()
                        st.rerun()
            else:
                st.info("Aún no hay movimientos registrados.")

            # ── Eliminar posiciones ────────────────────────────────────────────
            st.markdown("---")
            with st.expander("🛠️ Gestionar / Eliminar Posiciones"):
                st.warning("Seleccioná las filas que deseás eliminar.")
                cols_vis = ['Ticker', 'Cantidad', 'Cartera']
                if all(c in df.columns for c in cols_vis):
                    df_borrar = df[cols_vis].copy()
                    df_borrar['Eliminar'] = False
                    editado = st.data_editor(
                        df_borrar,
                        column_config={"Eliminar": st.column_config.CheckboxColumn(required=True)},
                        disabled=["Ticker", "Cantidad", "Cartera"],
                        hide_index=True,
                        use_container_width=True,
                    )
                    if st.button("Confirmar Eliminación", type="primary", use_container_width=True):
                        filas_borrar = editado[editado['Eliminar'] == True]
                        if not filas_borrar.empty:
                            for _, fila in filas_borrar.iterrows():
                                db.execute(
                                    "DELETE FROM movimientos WHERE ticker=%s AND id_cartera=%s AND id_usuario=%s",
                                    (fila['Ticker'], fila['Cartera'], st.session_state.user_id)
                                )
                            st.success("✅ Registros y movimientos eliminados.")
                            st.cache_data.clear()
                            st.rerun()

    # ==========================================================================
    # TAB 2 — POSICIONES CERRADAS
    # ==========================================================================
    with tab_cerr:
        st.subheader("🔒 Posiciones Cerradas")
        st.caption(
            "Las posiciones se calculan automáticamente desde los movimientos de VENTA. "
            "Para renta fija se incluye la renta y amortización cobrada. "
            "También podés cargar posiciones manualmente."
        )

        df_cerradas = load_posiciones_cerradas(st.session_state.user_id)

        if not df_cerradas.empty:
            total_gan_cerr = df_cerradas['Ganancia Total (USD)'].sum()
            col_r1, col_r2 = st.columns(2)
            col_r1.metric("Resultado total realizado (USD)", f"USD {total_gan_cerr:,.2f}")
            col_r2.metric("Operaciones cerradas", len(df_cerradas))

            def color_gan(val):
                if isinstance(val, (int, float)):
                    return 'color: #2ecc71' if val > 0 else 'color: #e74c3c' if val < 0 else ''
                return ''

            st.dataframe(
                df_cerradas.style.applymap(
                    color_gan,
                    subset=['Gan. Capital (USD)', 'Gan. Renta (USD)',
                            'Ganancia Total (USD)', 'ROI %']
                ).format({
                    'Cantidad':             '{:,.4f}',
                    'P. Compra (USD)':      '{:,.4f}',
                    'P. Venta (USD)':       '{:,.4f}',
                    'Gan. Capital (USD)':   '{:,.2f}',
                    'Gan. Renta (USD)':     '{:,.2f}',
                    'Ganancia Total (USD)': '{:,.2f}',
                    'ROI %':                '{:,.2f}%',
                }),
                use_container_width=True,
                hide_index=True,
            )

            buf_c = io.BytesIO()
            with pd.ExcelWriter(buf_c, engine='xlsxwriter') as writer:
                df_cerradas.to_excel(writer, index=False, sheet_name='Cerradas')
            st.download_button(
                "📥 Descargar Posiciones Cerradas (Excel)",
                data=buf_c.getvalue(),
                file_name=f"Cerradas_{st.session_state.user_name}.xlsx",
                mime="application/vnd.ms-excel",
                use_container_width=True,
            )
        else:
            st.info("Sin posiciones cerradas aún.")

        # Carga manual
        st.markdown("---")
        with st.expander("➕ Cargar posición cerrada manualmente"):
            with st.form("form_cerrada_manual"):
                fc1, fc2 = st.columns(2)
                with fc1:
                    cm_fecha  = st.date_input("Fecha de cierre", datetime.now())
                    cm_ticker = st.text_input("Ticker")
                    cm_activo = st.selectbox(
                        "Tipo de instrumento",
                        ["Acciones", "Cedears", "Bonos", "Obligaciones Negociables", "Otros"]
                    )
                    cm_cartera = st.text_input("Cartera", value="Personal")
                with fc2:
                    cm_cant   = st.number_input("Cantidad", min_value=0.0, step=0.0001)
                    cm_pcomp  = st.number_input("Precio compra (USD)", min_value=0.0)
                    cm_pventa = st.number_input("Precio venta (USD)",  min_value=0.0)
                    cm_renta  = st.number_input("Renta/Amort cobrada (USD)", min_value=0.0,
                                                help="Solo para renta fija")

                if st.form_submit_button("Guardar", use_container_width=True, type="primary"):
                    if cm_cant > 0 and cm_ticker:
                        gan_cap   = (cm_pventa - cm_pcomp) * cm_cant
                        gan_total = gan_cap + cm_renta
                        roi_m     = (gan_total / (cm_pcomp * cm_cant) * 100) if cm_pcomp > 0 else 0
                        db.execute("""
                            INSERT INTO posiciones_cerradas
                              (id_usuario, fecha, ticker, activo, cartera, cantidad,
                               precio_compra, precio_venta, moneda,
                               ganancia_capital, ganancia_renta, ganancia_total, roi)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """, (
                            st.session_state.user_id, str(cm_fecha),
                            cm_ticker.upper(), cm_activo, cm_cartera, cm_cant,
                            cm_pcomp, cm_pventa, 'USD',
                            round(gan_cap, 2), round(cm_renta, 2),
                            round(gan_total, 2), round(roi_m, 2)
                        ))
                        st.success(f"✅ Posición cerrada de {cm_ticker.upper()} guardada.")
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.warning("Completá al menos Ticker y Cantidad.")

    # ==========================================================================
    # TAB 3 — NUEVA OPERACIÓN
    # ==========================================================================
    with tab_op:
        st.markdown("### Registrar Movimiento")

        with st.popover("➕ Nueva Operación", use_container_width=True):
            st.markdown("### 📝 Registro de Movimiento")
            t_op = st.selectbox(
                "Operación",
                ["INGRESO", "EGRESO", "COMPRA", "VENTA", "RENTA", "AMORTIZACIÓN", "SPLIT"],
                key="tipo_op_principal"
            )
            f_op = st.date_input("Fecha", datetime.now(), key="fecha_operacion")

            tickers_master = db.run_query(
                "SELECT ticker FROM master_tickers WHERE ticker NOT LIKE 'CASH%' ORDER BY ticker"
            )['ticker'].tolist()

            if t_op in ["INGRESO", "EGRESO"]:
                opciones = ["CASH"]
                esta_deshabilitado = True
            else:
                opciones = tickers_master
                esta_deshabilitado = False

            a_op = st.selectbox(
                "Ticker / Activo",
                opciones,
                disabled=esta_deshabilitado,
                key=f"ticker_activo_{t_op}",
                help=(
                    "El CASH se guarda separado por moneda (CASH_ARS / CASH_USD) "
                    "según lo que elijas en 'Moneda' más abajo, para no mezclar "
                    "pesos y dólares en un mismo total."
                    if t_op in ["INGRESO", "EGRESO"] else None
                )
            )

            # Leer carteras desde inversiones (incluye metas nuevas sin movimientos)
            df_carteras_inv = db.run_query(
                "SELECT DISTINCT cartera FROM inversiones WHERE id_usuario = %(uid)s AND cartera IS NOT NULL",
                {"uid": st.session_state.user_id}
            )
            carteras_inv = df_carteras_inv['cartera'].tolist() if not df_carteras_inv.empty else []
            carteras_mov = list(df['Cartera'].unique()) if not df.empty else []
            list_carteras = sorted(list(set(carteras_inv + carteras_mov))) or ["Personal"]
            cart_op = st.selectbox("Cartera destino:", list_carteras)

            c3, c4 = st.columns(2)
            with c3:
                m_op = st.selectbox("Moneda", ["USD", "ARS"],
                                    index=0 if moneda_visualizacion == "USD" else 1)
            with c4:
                q_op = st.number_input("Cantidad / Monto", min_value=0.0,
                                       step=0.00001, format="%.5f")

            p_op = st.number_input("Precio Unitario (en la moneda elegida)", min_value=0.0)

            afectar_cash = True
            if t_op in ["COMPRA", "VENTA"]:
                afectar_cash = st.checkbox(
                    "💵 Descontar / acreditar el monto en CASH automáticamente",
                    value=True,
                    help="Si lo desmarcás, tenés que cargar el EGRESO/INGRESO de CASH vos "
                         "manualmente en otra operación."
                )

            if st.button("Guardar Registro", use_container_width=True, type="primary"):
                if q_op > 0:
                    cantidad_final = (
                        q_op if t_op in ["INGRESO", "COMPRA", "RENTA", "AMORTIZACIÓN"]
                        else -q_op
                    )
                    tc_para_guardar = tc_ccl  # guardamos el CCL como referencia histórica

                    # Ticker efectivo a guardar: el CASH se separa por moneda
                    # (CASH_ARS / CASH_USD) para no mezclar pesos y dólares.
                    ticker_final = f"CASH_{m_op}" if t_op in ["INGRESO", "EGRESO"] else a_op

                    db.execute("""
                        INSERT INTO movimientos
                          (id_usuario, fecha, ticker, tipo_operacion,
                           cantidad, precio_unitario, moneda, ccl_al_dia, id_cartera)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (
                        st.session_state.user_id, str(f_op), ticker_final, t_op,
                        cantidad_final, p_op, m_op, tc_para_guardar, cart_op
                    ))

                    # Descontar/acreditar CASH automáticamente en COMPRA / VENTA
                    if t_op in ["COMPRA", "VENTA"] and afectar_cash:
                        monto_total = round(q_op * p_op, 5)
                        tipo_cash = "EGRESO" if t_op == "COMPRA" else "INGRESO"
                        cantidad_cash = -monto_total if tipo_cash == "EGRESO" else monto_total
                        db.execute("""
                            INSERT INTO movimientos
                              (id_usuario, fecha, ticker, tipo_operacion,
                               cantidad, precio_unitario, moneda, ccl_al_dia, id_cartera)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """, (
                            st.session_state.user_id, str(f_op), f"CASH_{m_op}", tipo_cash,
                            cantidad_cash, 1, m_op, tc_para_guardar, cart_op
                        ))

                    st.success(f"✅ ¡{t_op} de {a_op} registrado con éxito!")
                    st.cache_data.clear()
                    st.rerun()
                else:
                    st.warning("La cantidad debe ser mayor a 0.")

    # ==========================================================================
    # TAB 4 — METAS
    # ==========================================================================
    with tab_metas:
        st.subheader("🎯 Gestión de Metas / Carteras")
        n_cartera = st.text_input("Nombre de la nueva meta:")
        if st.button("Crear Cartera"):
            if n_cartera:
                db.execute(
                    "INSERT INTO inversiones (id_usuario, ticker, cantidad, tipo, cartera) VALUES (%s, 'CASH', 0, 'EFECTIVO', %s)",
                    (st.session_state.user_id, n_cartera)
                )
                st.success("Meta creada!")
                st.rerun()

    # ==========================================================================
    # TAB 5 — ADMIN MASTER (solo admin)
    # ==========================================================================
    if es_admin:
        with tab_admin:
            st.header("🔧 Panel de Control de Ratios")
            st.info("Cualquier cambio aquí afectará los cálculos de TODOS los clientes en tiempo real.")

            df_master = db.run_query("SELECT * FROM master_tickers ORDER BY ticker")

            st.subheader("Editar Master Tickers (Precios y Ratios)")
            editado_master = st.data_editor(
                df_master,
                num_rows="dynamic",
                key="editor_master_admin",
                use_container_width=True,
                hide_index=True,
            )
            if st.button("Guardar Cambios en Master", type="primary"):
                db.df_to_table(editado_master, 'master_tickers', if_exists='replace')
                st.success("✅ Master Tickers actualizado.")
                st.cache_data.clear()
                st.rerun()
            else:
                conn7.close()


