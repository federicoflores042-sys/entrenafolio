import streamlit as st
from sqlalchemy import create_engine, text
import pandas as pd

# 1. Función de conexión centralizada usando SQLAlchemy
def get_engine():
    # Usamos la clave que pegamos en los Secrets de Streamlit
    return create_engine(st.secrets["DB_URL"])

# 2. Función de validación para Neon
def validar_login(usuario, contrasena):
    engine = get_engine()
    u_limpio = usuario.strip().lower()
    p_limpia = contrasena.strip()
    
    # Buscamos en la tabla 'usuarios' que creamos en Neon
    query = text("SELECT id FROM usuarios WHERE nombre_usuario = :u AND contrasena = :p")
    
    try:
        with engine.connect() as conn:
            result = conn.execute(query, {"u": u_limpio, "p": p_limpia}).fetchone()
            return result[0] if result else None
    except Exception as e:
        st.error(f"Error de conexión a la base: {e}")
        return None

# 3. Inicializar sesión
if 'user_id' not in st.session_state:
    st.session_state.user_id = None

# 4. Interfaz Principal
if st.session_state.user_id is None:
    st.title("🚀 Bienvenido a Entrenafolio Pro")
    
    tab1, tab2 = st.tabs(["Ingresar", "Registrarme"])

    with tab1:
        with st.form("Login"):
            u_input = st.text_input("Usuario")
            p_input = st.text_input("Contraseña", type="password")
            if st.form_submit_button("Ingresar"):
                id_user = validar_login(u_input, p_input)
                if id_user:
                    st.session_state.user_id = id_user
                    st.rerun()
                else:
                    st.error("Usuario o contraseña incorrectos.")

    with tab2:
        with st.form("Registro"):
            nuevo_u = st.text_input("Elegí un Usuario").strip().lower()
            nuevo_p = st.text_input("Elegí una Contraseña", type="password").strip()
            if st.form_submit_button("Crear Cuenta"):
                if nuevo_u and nuevo_p:
                    try:
                        engine = get_engine()
                        with engine.begin() as conn:
                            conn.execute(
                                text("INSERT INTO usuarios (nombre_usuario, contrasena) VALUES (:u, :p)"),
                                {"u": nuevo_u, "p": nuevo_p}
                            )
                        st.success("¡Cuenta creada! Ya podés ingresar.")
                    except Exception:
                        st.error("Ese nombre de usuario ya existe.")
                else:
                    st.warning("Completá todos los campos.")

else:
    # --- SESIÓN INICIADA ---
    st.sidebar.title(f"👤 Usuario ID: {st.session_state.user_id}")
    if st.sidebar.button("Cerrar Sesión"):
        st.session_state.user_id = None
        st.rerun()

    st.header("📈 Mi Portafolio en la Nube")

    # --- FORMULARIO DE CARGA PARA NEON ---
    with st.expander("➕ Cargar nueva inversión"):
        with st.form("carga_activos"):
            ticker = st.text_input("Ticker (ej: ALUA, AAPL, BTC)").upper()
            cantidad = st.number_input("Cantidad", min_value=0.0, step=0.1)
            precio = st.number_input("Precio de Compra", min_value=0.0)
            
            if st.form_submit_button("Guardar en Neon"):
                if ticker and cantidad > 0:
                    engine = get_engine()
                    with engine.begin() as conn:
                        conn.execute(
                            text("INSERT INTO inversiones (usuario_id, ticket, cantidad, precio_compra) VALUES (:uid, :t, :c, :p)"),
                            {"uid": st.session_state.user_id, "t": ticker, "c": cantidad, "p": precio}
                        )
                    st.success(f"¡{ticker} guardado para siempre!")
                    st.rerun()

    # --- VISUALIZACIÓN DESDE NEON ---
    st.subheader("Tus Activos Registrados")
    engine = get_engine()
    query = text("SELECT ticket, cantidad, precio_compra, fecha FROM inversiones WHERE usuario_id = :uid")
    
    with engine.connect() as conn:
        df_user = pd.read_sql(query, conn, params={"uid": st.session_state.user_id})

    if not df_user.empty:
        st.dataframe(df_user, use_container_width=True)
    else:
        st.info("Tu portafolio está vacío. ¡Cargá tu primer activo!")
