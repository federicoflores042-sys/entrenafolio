import streamlit as st
import pandas as pd
import sqlite3
import yfinance as yf
import plotly.express as px
from datetime import datetime
from iol_client import IOLClient

# --- 1. CONFIGURACIÓN DE PÁGINA ---
st.set_page_config(page_title="Entrenanfolio Pro", layout="wide", page_icon="📈")

# --- 2. CONEXIÓN IOL ---
try:
    iol = IOLClient(st.secrets["IOL_USER"], st.secrets["IOL_PASS"])
except Exception as e:
    iol = None

# --- 3. FUNCIONES DE BASE DE DATOS ---
def validar_login(usuario, password):
    conn = sqlite3.connect('entrenanfolio.db')
    cursor = conn.cursor()
    u_limpio = usuario.strip().lower()
    query = "SELECT id_usuario, usuario FROM usuarios WHERE LOWER(usuario) = ? AND password = ?"
    cursor.execute(query, (u_limpio, password.strip()))
    resultado = cursor.fetchone()
    conn.close()
    return resultado if resultado else None

def registrar_usuario(usuario, password):
    conn = sqlite3.connect('entrenanfolio.db')
    cursor = conn.cursor()
    u_limpio = usuario.strip().lower()
    try:
        cursor.execute("INSERT INTO usuarios (usuario, password) VALUES (?, ?)", (u_limpio, password.strip()))
        conn.commit()
        exito = True
    except sqlite3.IntegrityError:
        exito = False  # El usuario ya existe
    conn.close()
    return exito

@st.cache_data(ttl=60)
def load_data_sqlite(user_id):
    conn = sqlite3.connect('entrenanfolio.db')
    # JOIN con Master Tickers para traer ratios y tickers de Yahoo automáticamente
    query = f"""
        SELECT i.id_inversion, i.ticker as Ticker, i.cantidad as Cantidad, i.tipo as Activo, i.cartera as Cartera,
               i.costo_promedio as Costo_Unit_Compra,
               m.ratio as Ratio, m.ticker_yahoo as Ticker_Yahoo
        FROM inversiones i
        LEFT JOIN master_tickers m ON i.ticker = m.ticker
        WHERE i.id_usuario = {user_id}
    """
    df = pd.read_sql(query, conn)
    conn.close()
    return df

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

   # 1. Carga de datos desde SQLite
    df = load_data_sqlite(st.session_state.user_id)

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
                        conn = sqlite3.connect('entrenanfolio.db')
                        cursor = conn.cursor()
                        cursor.execute(f"DELETE FROM inversiones WHERE id_inversion IN ({','.join(map(str, ids_a_borrar))})")
                        conn.commit()
                        conn.close()
                        st.success(f"✅ Se eliminaron {len(ids_a_borrar)} registros.")
                        st.cache_data.clear()
                        st.rerun()
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
                conn = sqlite3.connect('entrenanfolio.db')
                tickers_master = pd.read_sql("SELECT ticker FROM master_tickers ORDER BY ticker", conn)['ticker'].tolist()
                conn.close()
                
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
                        conn = sqlite3.connect('entrenanfolio.db')
                        conn.execute("""
                            INSERT INTO inversiones (id_usuario, ticker, cantidad, tipo, cartera, costo_promedio) 
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (st.session_state.user_id, a_op, q_op, cat_op, cart_op, cp_op))
                        conn.commit()
                        conn.close()
                        st.success(f"✅ ¡{a_op} guardado!"); st.cache_data.clear(); st.rerun()

    with tab_metas:
        st.subheader("🎯 Gestión de Metas")
        n_cartera = st.text_input("Nombre de la nueva meta:")
        if st.button("Crear Cartera"):
            if n_cartera:
                conn = sqlite3.connect('entrenanfolio.db')
                conn.execute("INSERT INTO inversiones (id_usuario, ticker, cantidad, tipo, cartera) VALUES (?, 'CASH', 0, 'EFECTIVO', ?)",
                             (st.session_state.user_id, n_cartera))
                conn.commit(); conn.close(); st.success("Meta creada!"); st.rerun()
                
                # --- PANEL DE ADMINISTRACIÓN (SOLO VISIBLE PARA VOS) ---
    if es_admin:
        with tab_admin:
            st.header("🔧 Panel de Control de Ratios")
            st.info("Cualquier cambio aquí afectará los cálculos de TODOS los clientes en tiempo real.")
            
            conn = sqlite3.connect('entrenanfolio.db')
            df_master = pd.read_sql("SELECT * FROM master_tickers ORDER BY ticker", conn)
            
            st.subheader("Editar Master Tickers")
            editado_master = st.data_editor(
                df_master,
                num_rows="dynamic",
                key="editor_master_admin",
                use_container_width=True,
                hide_index=True
            )
            
            if st.button("Guardar Cambios en Master", type="primary"):
                # Actualizamos la tabla maestra reemplazando con los nuevos datos del editor
                editado_master.to_sql('master_tickers', conn, if_exists='replace', index=False)
                conn.close()
                st.success("✅ Master Tickers actualizado. Los cambios ya se ven en todas las carteras.")
                st.cache_data.clear()
                st.rerun()