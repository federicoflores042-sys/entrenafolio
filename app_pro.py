import streamlit as st
import pandas as pd
import sqlite3
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
    # CAMBIO AQUÍ: Agregamos nombre_usuario a la consulta
    query = text("SELECT id, nombre_usuario FROM usuarios WHERE nombre_usuario = :u AND contrasena = :p")
    try:
        with engine.connect() as conn:
            result = conn.execute(query, {"u": u_limpio, "p": contrasena.strip()}).fetchone()
            # Ahora devuelve una fila con [id, nombre_usuario]
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
    # Ajustamos los nombres de columnas a los que creamos en Neon
    query = text("""
        SELECT id as id_inversion, ticket as Ticker, cantidad as Cantidad, 
               'Cedears' as Activo, 'Personal' as Cartera,
               precio_compra as Costo_Unit_Compra,
               1 as Ratio, ticket as Ticker_Yahoo
        FROM inversiones 
        WHERE usuario_id = :uid
    """)
    try:
        with engine.connect() as conn:
            df = pd.read_sql(query, conn, params={"uid": user_id})
            return df
    except Exception as e:
        st.error(f"Error al cargar datos de Neon: {e}")
        return pd.DataFrame()

# --- 4. MOTOR DE PRECIOS (TU LÓGICA ORIGINAL) ---
def obtener_ccl_real():
    try:
        # Forzamos la extracción del último precio como un número float puro
        ggal_l = float(yf.download("GGAL.BA", period="1d", interval="1m")['Close'].iloc[-1])
        ggal_a = float(yf.download("GGAL", period="1d", interval="1m")['Close'].iloc[-1])
        ccl = (ggal_l * 10) / ggal_a
        return round(ccl, 2)
    except: 
        return 1515.0

@st.cache_data(ttl=600) 
def obtener_precio_cached(ticker, ratio, ccl):
    # 1. Intento con IOL
    if iol:
        try:
            precio_iol = iol.obtener_precio(ticker.replace(".BA", "").strip().upper())
            if precio_iol and precio_iol > 0:
                if any(x in ticker for x in ["AE38", "AL30", "GD30"]): return float(precio_iol / 100.0)
                if ratio > 1: return float((precio_iol * ratio) / ccl)
                return float(precio_iol / ccl)
        except: pass
        
    # 2. Intento con Yahoo Finance (Ajustado para evitar el error de la imagen)
    try:
        tk_search = ticker if not any(x in ticker for x in ["AE38", "AL30"]) or ticker.endswith(".BA") else f"{ticker}.BA"
        asset = yf.Ticker(tk_search)
        
        # .fast_info['last_price'] es más directo, pero si falla usamos iloc[-1]
        try:
            precio = float(asset.fast_info['last_price'])
        except:
            precio = float(asset.history(period="1d")['Close'].iloc[-1])
            
        if any(x in ticker for x in ["AE38", "AL30"]) and precio > 5: 
         # Devolvemos el precio crudo para procesarlo con la lógica de ratios más abajo
         return precio
    except: 
        return 0.0

# --- 5. LÓGICA DE SESIÓN ---
if 'user_id' not in st.session_state:
    st.session_state.user_id = None

# --- 6. INTERFAZ: LOGIN O DASHBOARD ---
if st.session_state.user_id is None:
    st.title("🚀 Bienvenido a Entrenanfolio")
    
    # Selector para alternar entre Login y Registro
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
                        st.success("✅ ¡Cuenta creada! Ya podés ingresar en la otra pestaña.")
                    else:
                        st.error("⚠️ El nombre de usuario ya está en uso.")
else:
    # --- DASHBOARD LOGUEADO ---
    tc_conversion = obtener_ccl_real()
    
    # Sidebar
    st.sidebar.title(f"👤 {st.session_state.user_name}")
    moneda_visualizacion = st.sidebar.radio("Ver en:", ["USD", "ARS"], horizontal=True)
    if st.sidebar.button("Cerrar Sesión"):
        st.session_state.user_id = None
        st.rerun()
     # --- DEFINIR ADMIN ---
    es_admin = st.session_state.user_name.lower() == "federicoflores" 

   # 1. Carga de datos desde NEON
   df = load_data_neon(st.session_state.user_id)

    # 2. PROCESAMIENTO DE PRECIOS (Mezclado y corregido)
    if not df.empty:
        precios_mercado = {}
        with st.spinner('Actualizando cotizaciones...'):
            for _, row in df.iterrows():
                tk = row['Ticker_Yahoo'] if pd.notna(row['Ticker_Yahoo']) else row['Ticker']
                # Obtenemos el precio (Action Full en USD)
                precios_mercado[row['Ticker']] = obtener_precio_cached(tk, row['Ratio'] or 1, tc_conversion)

        # Mapeamos el precio de la acción completa
        df['Precio_Accion_Full'] = df['Ticker'].map(precios_mercado)
        
        # --- 1. DEFINICIÓN DE MULTIPLICADOR ---
        # Definimos el multiplicador de moneda antes de usarlo
        mult = tc_conversion if moneda_visualizacion == "ARS" else 1.0
        
        
        # --- 2. PROCESAMIENTO DE PRECIOS ACTUALES (Lógica diferenciada) ---
        def calcular_precio_unitario(row):
            precio_full = row['Precio_Accion_Full']
            ratio = row['Ratio'] if (pd.notna(row['Ratio']) and row['Ratio'] > 0) else 1
            
            if row['Activo'] in ['Obligaciones Negociables', 'Bonos', 'ON']:
                # CASO 1: El precio viene en PESOS (ej: 1015.0)
                if precio_full > 100:
                    return precio_full / tc_conversion
                
                # CASO 2: Yahoo devuelve una escala rara (como el 4.83 actual)
                # Si el precio es mayor a 2 pero menor a 10, suele ser un error de factor 7 u 8 de Yahoo
                elif 2.0 < precio_full < 10.0:
                    # Forzamos la paridad estimada o dividimos por el factor de error común
                    return (precio_full / 7.0) 
                
                # CASO 3: El precio viene como paridad 0-100 (ej: 68.5)
                elif precio_full > 5:
                    return precio_full / 100.0
                
                # CASO 4: Ya es una paridad 0-1 (ej: 0.68)
                else:
                    return precio_full
            
            elif row['Activo'] == 'Cedears':
                return precio_full / ratio
            else:
                return precio_full

        def calcular_costo_ajustado(row):
            costo = row['Costo_Unit_Compra']
            
            # Si el costo es mayor a 500, asumimos que se cargó en ARS y lo pasamos a USD
            if costo > 500:
                costo = costo / tc_conversion
            
            if row['Activo'] in ['Obligaciones Negociables', 'Bonos', 'ON']:
                # Ya convertido a USD, lo dejamos directo
                return costo 
            elif row['Activo'] == 'Cedears':
                ratio = row['Ratio'] if (pd.notna(row['Ratio']) and row['Ratio'] > 0) else 1
                return costo / ratio
            else:
                return costo

        # 1. Aplicamos las funciones de cálculo unitario
        df['Precio_USD_Unitario'] = df.apply(calcular_precio_unitario, axis=1)
        df['Costo_Unit_Ajustado'] = df.apply(calcular_costo_ajustado, axis=1)
        
        # 2. Definimos el precio visual y la VALUACIÓN (Esto corrige el KeyError)
        df['Precio_V'] = df['Precio_USD_Unitario'] * mult
        df['Valuacion_V'] = df['Precio_V'] * df['Cantidad']
        
        # 3. Limpieza de datos
        df['Valuacion_V'] = pd.to_numeric(df['Valuacion_V'], errors='coerce').fillna(0)
        df['Precio_V'] = pd.to_numeric(df['Precio_V'], errors='coerce').fillna(0)
        df['Costo_Unit_Compra'] = pd.to_numeric(df['Costo_Unit_Compra'], errors='coerce').fillna(0)

        # 4. CÁLCULOS DE RENDIMIENTO
        df['Inversion_Total_V'] = (df['Costo_Unit_Ajustado'] * df['Cantidad']) * mult
        
        # Ahora sí existe Valuacion_V para calcular la ganancia
        df['Ganancia_Nominal'] = df['Valuacion_V'] - df['Inversion_Total_V']
        df['ROI_%'] = (df['Ganancia_Nominal'] / df['Inversion_Total_V']) * 100
        df['ROI_%'] = df['ROI_%'].fillna(0).replace([float('inf'), float('-inf')], 0)

        # --- MÉTRICAS GLOBALES ---
        st.title(f"📊 Mi Portafolio ({moneda_visualizacion})")
        m1, m2, m3, m4 = st.columns(4) # Agregamos una cuarta columna para el ROI total
        
        total_patrimonio = float(df['Valuacion_V'].sum())
        total_ganancia = float(df['Ganancia_Nominal'].sum())
        # ROI promedio de la cartera ponderado por valuación
        roi_total = (total_ganancia / (total_patrimonio - total_ganancia)) * 100 if (total_patrimonio - total_ganancia) != 0 else 0
        
        simbolo_moneda = 'ARS' if moneda_visualizacion == 'ARS' else 'USD'
        
        m1.metric("Patrimonio Total", f"{simbolo_moneda} {total_patrimonio:,.2f}")
        m2.metric("Tipo de Cambio (CCL)", f"${tc_conversion}")
        m3.metric("Ganancia Total", f"{simbolo_moneda} {total_ganancia:,.2f}", delta=f"{total_ganancia:,.2f}")
        m4.metric("ROI Cartera", f"{roi_total:,.2f}%", delta=f"{roi_total:,.2f}%")
        
        
        
   # --- 3. PESTAÑAS (DINÁMICAS PARA ADMIN) ---
    titulos_tabs = ["💰 Ver Todo", "➕ Nueva Operación", "🎯 Nuevas Metas"]
    if es_admin:
        titulos_tabs.append("🛠️ Admin Master")

    tabs = st.tabs(titulos_tabs)
    tab_todo = tabs[0]
    tab_operar = tabs[1]
    tab_metas = tabs[2]
    if es_admin:
        tab_admin = tabs[3]

    with tab_todo:
        if not df.empty:
            st.subheader("📊 Detalle de Posiciones")
            
            # 1. Seleccionamos las columnas incluyendo el COSTO AJUSTADO
            df_display = df[[
                'Ticker', 'Cantidad', 'Activo', 'Costo_Unit_Ajustado', 
                'Precio_V', 'Valuacion_V', 'Ganancia_Nominal', 'ROI_%'
            ]].copy()

            # 2. Renombramos para la visualización
            df_display.columns = [
                'Ticker', 'Cant.', 'Tipo', 'Costo Cert.', 
                'Precio Mercado', 'Valuación', 'Ganancia', 'ROI %'
            ]

            # 3. Aplicamos el formato de moneda y los colores (Verde > 0, Rojo < 0)
            st.dataframe(
                df_display.style.format({
                    'Cant.': lambda x: f"{x:,.8f}" if any(c in str(df_display.loc[df_display['Cant.'] == x, 'Tipo'].values) for c in ['Cripto', 'Crypto']) else f"{x:,.2f}",
                    'Costo Cert.': '{:,.2f}',
                    'Precio Mercado': '{:,.2f}',
                    'Valuación': '{:,.2f}',
                    'Ganancia': '{:,.2f}',
                    'ROI %': '{:,.2f}%'
                }, na_rep="-").applymap(
                    lambda x: 'color: #2ecc71' if isinstance(x, (int, float)) and x > 0 else 'color: #e74c3c' if isinstance(x, (int, float)) and x < 0 else '', 
                    subset=['Ganancia', 'ROI %']
                ),
                use_container_width=True,
                hide_index=True
            )
            
            # 4. Gráfico de torta original
            fig = px.pie(df, values='Valuacion_V', names='Ticker', hole=0.4, template="plotly_dark")
            st.plotly_chart(fig, use_container_width=True)

            # --- GESTOR PARA ELIMINAR ---
            st.markdown("---")
            with st.expander("🛠️ Gestionar / Eliminar Posiciones"):
                st.warning("Seleccioná las filas que deseás eliminar.")
                # Aquí usamos 'id_inversion' que ahora sí traemos en el SELECT de SQL
                df_borrar = df[['id_inversion', 'Ticker', 'Cantidad', 'Cartera']].copy()
                df_borrar['Eliminar'] = False
                
                editado = st.data_editor(
                    df_borrar,
                    column_config={"Eliminar": st.column_config.CheckboxColumn(required=True)},
                    disabled=["id_inversion", "Ticker", "Cantidad", "Cartera"],
                    hide_index=True,
                    use_container_width=True
                )

               if st.button("Confirmar Eliminación", type="primary", use_container_width=True):
                    ids_a_borrar = editado[editado['Eliminar'] == True]['id_inversion'].tolist()
                    if ids_a_borrar:
                        engine = create_engine(st.secrets["DB_URL"])
                        # En Neon la columna de la tabla inversiones es 'id'
                        query = text("DELETE FROM inversiones WHERE id IN :ids")
                        try:
                            with engine.begin() as conn:
                                conn.execute(query, {"ids": tuple(ids_a_borrar)})
                            st.success(f"✅ Se eliminaron {len(ids_a_borrar)} registros de la nube.")
                            st.cache_data.clear()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error al eliminar en Neon: {e}")
    with tab_operar:
        st.markdown("### Registrar Movimiento")
        # El popover debe estar indentado dentro de tab_operar
        with st.popover("➕ Nueva Operación", use_container_width=True):
            with st.form("form_movimientos_sql", clear_on_submit=True):
                st.markdown(f"### 📝 Registro en {moneda_visualizacion}")
                
                f_op = st.date_input("Fecha", datetime.now())
                t_op = st.selectbox("Operación", ["Compra", "Venta", "Dividendo"])
                cat_op = st.selectbox("Categoría", ["Cedears", "Acciones", "Criptomonedas", "Bonos","Obligaciones Negociables"])
                
                # Conexión rápida para buscar tickers
                engine = create_engine(st.secrets["DB_URL"])
with engine.connect() as conn:
    tickers_master = pd.read_sql(text("SELECT ticker FROM master_tickers ORDER BY ticker"), conn)['ticker'].tolist()
                a_op = st.selectbox("Ticker", tickers_master)
                

                list_carteras = sorted(list(df['Cartera'].unique())) if not df.empty else ["Personal"]
                cart_op = st.selectbox("Cartera destino:", list_carteras)
                
                c3, c4 = st.columns(2)
                with c3:
                    m_op = st.selectbox("Moneda", ["USD", "ARS"], index=0 if moneda_visualizacion == "USD" else 1)
                with c4:
                    q_op = st.number_input("Cantidad", min_value=0.0, step=0.00001, format="%.5f")
                
                col_p1, col_p2 = st.columns(2)
                with col_p1:
                    p_op = st.number_input(f"Precio Unitario", min_value=0.0)
                with col_p2:
                    cp_op = st.number_input(f"Costo Promedio", min_value=0.0)

                submit = st.form_submit_button("Guardar Registro", use_container_width=True, type="primary")
                
              if submit:
                    if q_op > 0:
                        engine = create_engine(st.secrets["DB_URL"])
                        query = text("""
                            INSERT INTO inversiones (usuario_id, ticket, cantidad, precio_compra) 
                            VALUES (:uid, :t, :c, :p)
                        """)
                        try:
                            with engine.begin() as conn:
                                conn.execute(query, {
                                    "uid": st.session_state.user_id, 
                                    "t": a_op, 
                                    "c": q_op, 
                                    "p": cp_op
                                })
                            st.success(f"✅ ¡{a_op} guardado en Neon!")
                            st.cache_data.clear()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error al guardar: {e}")

    with tab_metas:
        st.subheader("🎯 Gestión de Metas")
        n_cartera = st.text_input("Nombre de la nueva meta:")
       if st.button("Crear Cartera"):
            if n_cartera:
                engine = create_engine(st.secrets["DB_URL"])
                # Ajustamos a los nombres de Neon: usuario_id, ticket, cantidad, precio_compra
                query = text("INSERT INTO inversiones (usuario_id, ticket, cantidad, precio_compra) VALUES (:uid, 'CASH', 0, 0)")
                try:
                    with engine.begin() as conn:
                        conn.execute(query, {"uid": st.session_state.user_id})
                    st.success("¡Meta creada en la nube!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
                
                # --- PANEL DE ADMINISTRACIÓN (SOLO VISIBLE PARA VOS) ---
    if es_admin:
        with tab_admin:
            st.header("🔧 Panel de Control de Ratios (Nube)")
            st.info("Cualquier cambio aquí afectará los cálculos de TODOS los clientes en tiempo real.")
            
            # 1. Crear el motor de conexión a Neon
            engine = create_engine(st.secrets["DB_URL"])
            
            # 2. Leer los tickers desde la nube
            try:
                with engine.connect() as conn:
                    df_master = pd.read_sql(text("SELECT * FROM master_tickers ORDER BY ticker"), conn)
            except Exception as e:
                st.error(f"Error al leer Master Tickers: {e}")
                df_master = pd.DataFrame()
            
            st.subheader("Editar Master Tickers")
            editado_master = st.data_editor(
                df_master,
                num_rows="dynamic",
                key="editor_master_admin",
                use_container_width=True,
                hide_index=True
            )
            
            # 3. Guardar los cambios de vuelta a Neon
            if st.button("Guardar Cambios en Master", type="primary"):
                try:
                    # 'if_exists=replace' sobrescribe la tabla con los nuevos datos
                    editado_master.to_sql('master_tickers', engine, if_exists='replace', index=False)
                    st.success("✅ Master Tickers actualizado en Neon. Los cambios ya son visibles para todos.")
                    st.cache_data.clear()
                    st.rerun()
                except Exception as e:
                    st.error(f"Error al guardar en la nube: {e}")
