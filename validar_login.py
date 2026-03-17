import streamlit as st
import sqlite3
import pandas as pd

# 1. Función de validación mejorada
def validar_login(usuario, password):
    conn = sqlite3.connect('entrenanfolio.db')
    cursor = conn.cursor()
    u_limpio = usuario.strip().lower()
    p_limpia = password.strip()
    
    query = "SELECT id_usuario FROM usuarios WHERE LOWER(usuario) = ? AND password = ?"
    cursor.execute(query, (u_limpio, p_limpia))
    resultado = cursor.fetchone()
    conn.close()
    return resultado[0] if resultado else None

# 2. Inicializar sesión
if 'user_id' not in st.session_state:
    st.session_state.user_id = None

# 3. Interfaz Principal
if st.session_state.user_id is None:
    st.title("🚀 Bienvenido a Entrenanfolio")
    
    with st.expander("🔍 Ver datos reales en la DB"):
        conn = sqlite3.connect('entrenanfolio.db')
        df_debug = pd.read_sql("SELECT * FROM usuarios", conn)
        st.dataframe(df_debug)
        conn.close()

    with st.form("Login"):
        u_input = st.text_input("Usuario")
        p_input = st.text_input("Contraseña", type="password")
        
        if st.form_submit_button("Ingresar"):
            id_user = validar_login(u_input, p_input)
            if id_user:
                st.session_state.user_id = id_user
                st.rerun()
            else:
                st.error(f"Error: No se encontró coincidencia para '{u_input}'")

else:
    # --- TODO ESTO SOLO SE EJECUTA SI EL USUARIO ESTÁ LOGUEADO ---
    st.title(f"✅ ¡Entraste! ID de Cliente: {st.session_state.user_id}")
    
    if st.sidebar.button("Cerrar Sesión"):
        st.session_state.user_id = None
        st.rerun()

    st.header("📈 Mi Portafolio Personal")

    # --- FORMULARIO DE CARGA ---
    with st.expander("➕ Cargar nueva inversión"):
        with st.form("carga_activos"):
            ticker = st.text_input("Ticker del Activo (ej: ALUA, AAPL, BTC)").upper()
            cantidad = st.number_input("Cantidad comprada", min_value=0.0, step=0.1)
            tipo = st.selectbox("Categoría", ["CEDEAR", "Acción", "Crypto", "Bono", "FCI"])
            
            if st.form_submit_button("Guardar en Portafolio"):
                if ticker and cantidad > 0:
                    conn = sqlite3.connect('entrenanfolio.db')
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO inversiones (id_usuario, ticker, cantidad, tipo) 
                        VALUES (?, ?, ?, ?)
                    """, (st.session_state.user_id, ticker, cantidad, tipo))
                    conn.commit()
                    conn.close()
                    st.success(f"¡{ticker} cargado con éxito!")
                    st.rerun()
                else:
                    st.warning("Completá el ticker y la cantidad.")

    # --- VISUALIZACIÓN DEL PORTAFOLIO ---
    st.subheader("Tus Activos Registrados")
    conn = sqlite3.connect('entrenanfolio.db')
    # El filtrado ahora es seguro porque estamos dentro del 'else' del login
    query = f"SELECT ticker, cantidad, tipo FROM inversiones WHERE id_usuario = {st.session_state.user_id}"
    df_user = pd.read_sql(query, conn)
    conn.close()

    if not df_user.empty:
        st.dataframe(df_user, use_container_width=True)
    else:
        st.info("Aún no tenés activos cargados. ¡Usá el formulario de arriba!")