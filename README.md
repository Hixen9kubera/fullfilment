# Kubera Fulfillment

Dashboard interno de stock y ventas: cruza inventario de **MercadoLibre (Full)** con **Odoo** y muestra ranking de productos estrella.

- `GET /` – dashboard combinado (stock ML Full + stock Odoo + ventas del mes)
- `GET /estrella` – ranking histórico de productos más vendidos (Pareto)
- APIs JSON bajo `/api/ml/*`, `/api/odoo/*`, `/api/dashboard/*`

---

## Requisitos

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) para manejo de deps y runner
- Acceso al repo hermano `sales-dashboard/` (el módulo `ml_token_manager` y el `.env` se leen desde ahí — ver sección siguiente)
- Credenciales de Odoo y tokens de ML cargados en la base de tokens (`ml_tokens_dashboard`)

---

## Estructura esperada

Este repo **no es standalone**: depende de `sales-dashboard/` que debe estar como carpeta hermana.

```
kubera/
├── fulfillment/         ← este repo
│   ├── main.py
│   ├── ml_stock.py
│   ├── ml_ventas.py
│   ├── odoo_stock.py
│   ├── productos_estrella.py
│   ├── config.py
│   └── templates/
└── sales-dashboard/     ← NO incluido aquí (repo aparte)
    ├── .env             ← variables de entorno
    └── ml_token_manager.py
```

El [config.py](config.py) resuelve `../sales-dashboard/.env` y `../sales-dashboard/ml_token_manager.py` relativos a la carpeta `fulfillment/`.

---

## Setup para tu equipo

### 1. Clonar ambos repos como carpetas hermanas

```bash
mkdir -p ~/dev/kubera && cd ~/dev/kubera
git clone https://github.com/joseKubera/kubera-fulfillment.git fulfillment
git clone <URL-del-sales-dashboard> sales-dashboard
```

### 2. Configurar `.env` en `sales-dashboard/`

Pedile al admin las credenciales. Como mínimo:

```env
ODOO_URL=https://<instancia>.odoo.com
ODOO_DB=<db>
ODOO_USER=<usuario>
ODOO_PASSWORD=<password>

# Las que use ml_token_manager (Supabase u otro backend de tokens)
# Ejemplo:
SUPABASE_URL=...
SUPABASE_KEY=...
```

### 3. Instalar dependencias

Desde `kubera/` (la raíz, no dentro de `fulfillment/`):

```bash
cd ~/dev/kubera
uv sync
```

Si no hay `pyproject.toml` aún en esa raíz, instalar manualmente:

```bash
uv pip install fastapi uvicorn jinja2 python-dotenv supabase requests
```

---

## Correr el dashboard

Desde la **raíz** `kubera/` (importante: no desde `fulfillment/`, porque `main.py` usa imports relativos al paquete):

```bash
cd ~/dev/kubera
uv run uvicorn fulfillment.main:app --reload --port 8001
```

Abrir en el browser:

- http://localhost:8001/ — dashboard de stock + ventas del mes
- http://localhost:8001/estrella — productos estrella (histórico)
- http://localhost:8001/docs — Swagger UI con todos los endpoints

---

## Script standalone: Productos Estrella (CLI)

Para correr el análisis histórico en terminal sin levantar el server:

```bash
cd ~/dev/kubera
uv run python fulfillment/productos_estrella.py
```

Imprime ranking por cuenta y un consolidado con análisis de Pareto (50/80/90%).

---

## Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/` | Dashboard HTML (stock + ventas) |
| GET | `/estrella` | Dashboard HTML (productos estrella) |
| GET | `/api/dashboard/ml` | JSON combinado stock ML + Odoo + ventas mensuales |
| GET | `/api/dashboard/estrella` | JSON histórico consolidado de ambas cuentas |
| GET | `/api/ml/stock` | Stock ML por cuenta (Full + no-Full) |
| GET | `/api/ml/ventas/7dias` | Ventas últimos 7 días por cuenta |
| GET | `/api/odoo/stock/sample` | Muestra de 10 ítems de stock en Odoo |

---

## Cuentas ML soportadas

Definidas en [config.py](config.py):

```python
CUENTAS = ["BEKURA", "SANCORFASHION"]
```

Para agregar una cuenta nueva: sumarla a `CUENTAS` y asegurar que su token esté cargado en la tabla `ml_tokens_dashboard`.

---

## Troubleshooting

**`ModuleNotFoundError: ml_token_manager`**
→ Falta la carpeta hermana `sales-dashboard/` o el archivo `ml_token_manager.py` dentro.

**`Odoo authentication failed`**
→ Revisar `ODOO_URL/DB/USER/PASSWORD` en `sales-dashboard/.env`.

**Stock Full vacío para una cuenta**
→ El token puede haber expirado. Refrescar con `ml_token_manager` desde `sales-dashboard`.

**Corro desde `fulfillment/` y tira `ImportError: attempted relative import`**
→ Correr siempre desde `kubera/` con `uvicorn fulfillment.main:app`, no desde adentro de la carpeta.

---

## Contacto

Dueño del repo: [@joseKubera](https://github.com/joseKubera). Cualquier cambio va por PR a `main`.
