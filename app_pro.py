import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
from datetime import datetime
from iol_client import IOLClient
from sqlalchemy import create_engine, text

# --- 1. CONFIGURACIÓN DE PÁGINA ---
st.set_page_config(page_title="Entrenanfolio Pro", layout="wide", page_icon="📈")

# --- 2. CONEXIÓN IOL ---
try:
    iol = IOLClient(st.secrets["IOL_USER"], st.secrets["IOL_PASS"])
except Exception as e:
    iol = None

# --- 3. FUNCIONES DE BASE DE DATOS ---
def validar_login(usuario, contrasena):
    engine = create_engine(st.secrets["DB_URL"])
    u_limpio = usuario.strip().lower()
    query = text("SELECT id, nombre_usuario FROM usuarios WHERE nombre_usuario = :u AND contrasena = :p")
    try:
        with engine.connect() as conn:
            result = conn.execute(query, {"u": u_limpio, "p": contrasena.strip()}).fetchone()
            return result if result else None
    except Exception as e:
        st.error(f"Error en login: {e}")
        return None

def registrar_usuario(usuario, contrasena):
    engine = create_engine(st.secrets["DB_URL"])
    u_limpio = usuario.strip().lower()
    query = text("INSERT INTO usuarios (nombre_usuario, contrasena) VALUES (:u, :p)")
    try:
        with engine.begin() as conn:
            conn.execute(query, {"u": u_limpio, "p": contrasena.strip()})
        return True
    except Exception as e:
        st.error(f"Error al registrar: {e}")
        return False

@st.cache_data(ttl=60)
def load_data_neon(user_id):
    engine = create_engine(st.secrets["DB_URL"])
    # JOIN con master_tickers para obtener Ratio, Activo y Ticker_Yahoo reales
    query = text("""
        SELECT i.id as id_inversion, i.ticket as Ticker, i.cantidad as Cantidad, 
               m.activo as Activo, i.cartera_destino as Cartera,
               i.precio_compra as Costo_Unit_Compra,
               m.ratio as Ratio, m.ticker_yahoo as Ticker_Yahoo,
               i.fecha_operacion, i.tipo_operacion, i.moneda_carga
        FROM inversiones i
        LEFT JOIN master_tickers m ON i.ticket = m.ticker
        WHERE i.usuario_id = :uid
    """)
    try:
        with engine.connect() as conn:
            df = pd.read_sql(query, conn, params={"uid": user_id})
            return df
    except Exception as e:
        st.error(f"Error al cargar datos de Neon: {e}")
        return pd.DataFrame()

# --- 4. MOTOR DE PRECIOS ---
def obtener_ccl_real():
    try:
        ggal_l = float(yf.download("GGAL.BA", period="1d", interval="1m")['Close'].iloc[-1])
        ggal_a = float(yf.download("GGAL", period="1d", interval="1m")['Close'].iloc[-1])
        ccl = (ggal_l * 10) / ggal_a
        return round(ccl, 2)
    except: 
        return 1515.0

@st.cache_data(ttl=600) 
def obtener_precio_cached(ticker, ratio, ccl):
    if iol:
        try:
            precio_iol = iol.obtener_precio(ticker.replace(".BA", "").strip().upper())
            if precio_iol and precio_iol > 0:
                if any(x in ticker for x in ["AE38", "AL30", "GD30"]): return float(precio_iol / 100.0)
                if ratio and ratio > 1: return float((precio_iol * ratio) / ccl)
                return float(precio_iol / ccl)
        except: pass
        
    try:
        asset = yf.Ticker(ticker)
        try:
            precio = float(asset.fast_info['last_price'])
        except:
            precio = float(asset.history(period="1d")['Close'].iloc[-1])
        return precio
    except: 
        return 0.0

# --- 5. LÓGICA DE SESIÓN ---
if 'user_id' not in st.session_state:
    st.session_state.user_id = None

# --- 6. INTERFAZ: LOGIN O DASHBOARD ---
if st.session_state.user_id is None:
    st.title("🚀 Bienvenido a Entrenanfolio Pro")
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
            new_u = st.text_input("Nuevo Usuario")
            new_p = st.text_input("Nueva Contraseña", type="password")
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
else:
    # --- DASHBOARD LOGUEADO ---
    tc_conversion = obtener_ccl_real()
    st.sidebar.title(f"👤 {st.session_state.user_name}")
    moneda_visualizacion = st.sidebar.radio("Ver en:", ["USD", "ARS"], horizontal=True)
    if st.sidebar.button("Cerrar Sesión"):
        st.session_state.user_id = None
        st.rerun()

    es_admin = st.session_state.user_name.lower() == "federicoflores" 
    df = load_data_neon(st.session_state.user_id)

    if not df.empty:
        precios_mercado = {}
        with st.spinner('Actualizando cotizaciones...'):
            for _, row in df.iterrows():
                tk = row['Ticker_Yahoo'] if pd.notna(row['Ticker_Yahoo']) else row['Ticker']
                precios_mercado[row['Ticker']] = obtener_precio_cached(tk, row['Ratio'], tc_conversion)

        df['Precio_Accion_Full'] = df['Ticker'].map(precios_mercado)
        mult = tc_conversion if moneda_visualizacion == "ARS" else 1.0
        
        def calcular_precio_unitario(row):
            precio_full = row['Precio_Accion_Full']
            ratio = row['Ratio'] if (pd.notna(row['Ratio']) and row['Ratio'] > 0) else 1
            if row['Activo'] in ['Obligaciones Negociables', 'Bonos', 'ON']:
                if precio_full > 100: return precio_full / tc_conversion
                elif 2.0 < precio_full < 10.0: return (precio_full / 7.0) 
                elif precio_full > 5: return precio_full / 100.0
                else: return precio_full
            elif row['Activo'] == 'Cedears':
                return precio_full / ratio
            else: return precio_full

        def calcular_costo_ajustado(row):
            costo = row['Costo_Unit_Compra']
            # Lógica para determinar si se cargó en ARS o USD basada en la nueva columna moneda_carga
            es_ars = row['moneda_carga'] == 'ARS' if pd.notna(row['moneda_carga']) else costo > 500
            
            if es_ars: costo = costo / tc_conversion
            
            if row['Activo'] == 'Cedears':
                ratio = row['Ratio'] if (pd.notna(row['Ratio']) and row['Ratio'] > 0) else 1
                return costo / ratio
            return costo

        df['Precio_USD_Unitario'] = df.apply(calcular_precio_unitario, axis=1)
        df['Costo_Unit_Ajustado'] = df.apply(calcular_costo_ajustado, axis=1)
        df['Precio_V'] = df['Precio_USD_Unitario'] * mult
        df['Valuacion_V'] = df['Precio_V'] * df['Cantidad']
        
        df['Valuacion_V'] = pd.to_numeric(df['Valuacion_V'], errors='coerce').fillna(0)
        df['Inversion_Total_V'] = (df['Costo_Unit_Ajustado'] * df['Cantidad']) * mult
        df['Ganancia_Nominal'] = df['Valuacion_V'] - df['Inversion_Total_V']
        df['ROI_%'] = (df['Ganancia_Nominal'] / df['Inversion_Total_V']) * 100 if df['Inversion_Total_V'].sum() != 0 else 0

        st.title(f"📊 Mi Portafolio ({moneda_visualizacion})")
        m1, m2, m3, m4 = st.columns(4)
        total_pat = float(df['Valuacion_V'].sum())
        total_gan = float(df['Ganancia_Nominal'].sum())
        roi_tot = (total_gan / (total_pat - total_gan)) * 100 if (total_pat - total_gan) != 0 else 0
        
        m1.metric("Patrimonio Total", f"{moneda_visualizacion} {total_pat:,.2f}")
        m2.metric("Tipo de Cambio (CCL)", f"${tc_conversion}")
        m3.metric("Ganancia Total", f"{moneda_visualizacion} {total_gan:,.2f}", delta=f"{total_gan:,.2f}")
        m4.metric("ROI Cartera", f"{roi_tot:,.2f}%", delta=f"{roi_tot:,.2f}%")

    # --- PESTAÑAS ---
    titulos_tabs = ["💰 Ver Todo", "➕ Nueva Operación", "🎯 Nuevas Metas"]
    if es_admin: titulos_tabs.append("🛠️ Admin Master")
    tabs = st.tabs(titulos_tabs)

    with tabs[0]:
        if not df.empty:
            st.subheader("📊 Detalle de Posiciones")
            df_display = df[['Ticker', 'Cantidad', 'Activo', 'Costo_Unit_Ajustado', 'Precio_V', 'Valuacion_V', 'Ganancia_Nominal', 'ROI_%']].copy()
            df_display.columns = ['Ticker', 'Cant.', 'Tipo', 'Costo Cert.', 'Precio Mercado', 'Valuación', 'Ganancia', 'ROI %']
            st.dataframe(df_display.style.format({'Costo Cert.': '{:,.2f}', 'Precio Mercado': '{:,.2f}', 'Valuación': '{:,.2f}', 'Ganancia': '{:,.2f}', 'ROI %': '{:,.2f}%'}), use_container_width=True, hide_index=True)
            st.plotly_chart(px.pie(df, values='Valuacion_V', names='Ticker', hole=0.4, template="plotly_dark"), use_container_width=True)

            with st.expander("🛠️ Gestionar / Eliminar Posiciones"):
                df_borrar = df[['id_inversion', 'Ticker', 'Cantidad', 'Cartera']].copy()
                df_borrar['Eliminar'] = False
                editado = st.data_editor(df_borrar, column_config={"Eliminar": st.column_config.CheckboxColumn(required=True)}, disabled=["id_inversion", "Ticker", "Cantidad", "Cartera"], hide_index=True, use_container_width=True)
                if st.button("Confirmar Eliminación", type="primary", use_container_width=True):
                    ids_a_borrar = editado[editado['Eliminar'] == True]['id_inversion'].tolist()
                    if ids_a_borrar:
                        engine = create_engine(st.secrets["DB_URL"])
                        try:
                            with engine.begin() as conn:
                                conn.execute(text("DELETE FROM inversiones WHERE id IN :ids"), {"ids": tuple(ids_a_borrar)})
                            st.success(f"Se eliminaron {len(ids_a_borrar)} registros.")
                            st.cache_data.clear()
                            st.rerun()
                        except Exception as e: st.error(f"Error: {e}")

    with tabs[1]:
        st.markdown(f"## 📝 Registro en {moneda_visualizacion}")
        with st.popover("➕ Registrar Operación", use_container_width=True):
            with st.form("form_op", clear_on_submit=True):
                
                f_op = st.date_input("Fecha", datetime.now())
                t_op = st.selectbox("Operación", ["Compra", "Venta", "Dividendo"])
                cat_op = st.selectbox("Categoría", ["Cedears", "Acciones", "Criptomonedas", "Bonos", "Obligaciones Negociables"])
                
                engine = create_engine(st.secrets["DB_URL"])
                try:
                    with engine.connect() as conn:
                        lista_t = pd.read_sql(text("SELECT ticker FROM master_tickers ORDER BY ticker"), conn)['ticker'].tolist()
                except: lista_t = ["CASH"]
                
                tk_op = st.selectbox("Ticker", lista_t)
                
                list_carteras = sorted(list(df['Cartera'].unique())) if not df.empty else ["Vida personal"]
                cart_op = st.selectbox("Cartera destino:", list_carteras)
                
                c3, c4 = st.columns(2)
                with c3:
                    mon_op = st.selectbox("Moneda", ["USD", "ARS"], index=0 if moneda_visualizacion == "USD" else 1)
                with c4:
                    q_op = st.number_input("Cantidad", min_value=0.0, step=0.00001, format="%.5f")
                
                col_p1, col_p2 = st.columns(2)
                with col_p1:
                    p_op = st.number_input("Precio Unitario", min_value=0.0)
                with col_p2:
                    cp_op = st.number_input("Costo Promedio", min_value=0.0)
                
                if st.form_submit_button("Guardar Registro", type="primary", use_container_width=True):
                    if q_op > 0:
                        query = text("""
                            INSERT INTO inversiones (usuario_id, ticket, cantidad, precio_compra, fecha_operacion, tipo_operacion, categoria, cartera_destino, moneda_carga, costo_promedio) 
                            VALUES (:u, :t, :c, :p, :f, :top, :cat, :cart, :mon, :cp)
                        """)
                        try:
                            with engine.begin() as conn:
                                conn.execute(query, {
                                    "u": st.session_state.user_id, "t": tk_op, "c": q_op, "p": p_op,
                                    "f": f_op, "top": t_op, "cat": cat_op, "cart": cart_op, "mon": mon_op, "cp": cp_op
                                })
                            st.success(f"✅ ¡{tk_op} guardado!")
                            st.cache_data.clear()
                            st.rerun()
                        except Exception as e: st.error(f"Error: {e}")

    with tabs[2]:
        st.subheader("🎯 Gestión de Metas")
        n_cartera = st.text_input("Nombre de la nueva meta:")
        if st.button("Crear Cartera") and n_cartera:
            engine = create_engine(st.secrets["DB_URL"])
            try:
                with engine.begin() as conn:
                    conn.execute(text("INSERT INTO inversiones (usuario_id, ticket, cantidad, precio_compra, cartera_destino) VALUES (:uid, 'CASH', 0, 0, :cart)"), 
                                 {"uid": st.session_state.user_id, "cart": n_cartera})
                st.success("¡Meta creada!")
                st.rerun()
            except Exception as e: st.error(f"Error: {e}")

    if es_admin:
        with tabs[3]:
            st.header("🔧 Panel de Control de Ratios (Nube)")
            engine = create_engine(st.secrets["DB_URL"])
            with engine.connect() as conn:
                df_master = pd.read_sql(text("SELECT * FROM master_tickers ORDER BY ticker"), conn)
            editado_master = st.data_editor(df_master, num_rows="dynamic", use_container_width=True, hide_index=True)
            if st.button("Guardar Cambios en Master", type="primary"):
                try:
                    editado_master.to_sql('master_tickers', engine, if_exists='replace', index=False)
                    st.success("✅ Master Tickers actualizado en Neon.")
                    st.cache_data.clear()
                    st.rerun()
                except Exception as e: st.error(f"Error: {e}")
