import requests

class IOLClient:
    def __init__(self, username, password):
        self.url_base = "https://api.invertironline.com/"
        self.username = username
        self.password = password
        self.token = self._get_token()

    def _get_token(self):
        payload = {'username': self.username, 'password': self.password, 'grant_type': 'password'}
        try:
            response = requests.post(f"{self.url_base}token", data=payload)
            return response.json().get('access_token')
        except:
            return None

    def obtener_precio(self, simbolo):
        """Busca el precio en IOL (Mercado Argentino por defecto)"""
        if not self.token: return None
        headers = {'Authorization': f'Bearer {self.token}'}
        # Probamos primero en BCBA (Bonos, ONs, Acciones)
        url = f"{self.url_base}api/v2/Titulos/{simbolo}/Cotizacion"
        try:
            res = requests.get(url, headers=headers, params={'mercado': 'BCBA'})
            if res.status_code == 200:
                return res.json().get('ultimoPrecio')
        except:
            return None
        
  # --- ESTA ES LA PARTE QUE DEBES INTEGRAR AL FINAL ---
if __name__ == "__main__":
    # Prueba rápida en terminal (sin Streamlit)
    # Reemplaza con tus credenciales reales solo para probar
    USER_TEST = "nicorabbia03@gmail.com" 
    PASS_TEST = "Nr17092002#"
    
    print("🔄 Probando conexión con IOL...")
    client = IOLClient(USER_TEST, PASS_TEST)
    
    if client.token:
        precio = client.obtener_precio("AE38D")
        print(f"✅ Conexión exitosa.")
        print(f"📈 El precio del AE38D en IOL es: USD {precio}")
    else:
        print("❌ No se pudo obtener el token. Revisa tus credenciales.")      