# Biblioteca

Catálogo web de nuestros libros físicos. Corre en la Raspberry Pi y se usa desde el celular a través de Tailscale.

- **Dos bibliotecas:** Principal (estilo oscuro) y Niños (estilo colorido). Cada libro pertenece a una, el catálogo se puede ver por separado y puedes mover libros de una a otra seleccionándolos.
- **Foto de estante:** sacas una foto a una repisa (o eliges una de la galería, ya recortada), Claude lee los lomos, la app busca cada libro en Open Library (portada, ISBN, año) y tú confirmas antes de guardar.
- **Código de barras:** para un libro suelto, sácale una foto al código de la contratapa. Se lee en el celular (no usa la API de Claude) y los datos se buscan en Open Library y Google Books.
- **Varios códigos:** abre la cámara y pasa los libros uno tras otro; cada código se suma a una lista con un pitido. Al final se buscan todos juntos y se revisan en una sola pantalla antes de agregarlos. La cámara en vivo necesita abrir la app con https (Tailscale Serve); sin https se saca una foto por libro, que igual se suma a la lista sin esperar.
- **Foto de la portada:** si un ISBN no aparece en Open Library ni en Google Books, sácale una foto a la portada (y si quieres, a la página legal, la de los datos de impresión). Claude lee título, autor, editorial, año e ISBN, y la foto queda como portada. Está en la pantalla del código de barras, en la revisión de varios códigos o de una foto de estante, y en la ficha de un libro al que le faltan datos.
- **Repetidos:** en el menú "⋯", Repetidos muestra los libros que parecen el mismo y deja unirlos en uno, sin perder préstamos ni notas.
- **Completar datos:** en la ficha de un libro con pocos datos, escanea su código de barras para completar autor, editorial, año y portada.
- **Importar desde StoryGraph o Goodreads:** sube el CSV que exporta cualquiera de los dos. Se pueden importar solo los marcados como propios.
- **Catálogo compartido:** cada persona tiene su usuario y las dos pueden agregar, editar y borrar.
- **Búsqueda y estantes:** busca por título, autor o ISBN y filtra por estante.
- **Exportar:** descarga todo el catálogo en CSV cuando quieras.

## Instalar en la Raspberry Pi

Necesita Python 3.10 o más nuevo. Revísalo con `python3 --version` (Raspberry Pi OS Bookworm trae 3.11).

```bash
# 1. Bajar el código
cd ~
git clone https://github.com/arehermann/biblioteca.git
cd biblioteca

# 2. Instalar dependencias
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Configurar
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # copia el resultado en BIBLIOTECA_SECRET
nano .env                                                   # pega también tu ANTHROPIC_API_KEY

# 4. Crear los usuarios (te pide la contraseña)
.venv/bin/python manage.py crear-usuario anya
.venv/bin/python manage.py crear-usuario <usuario-de-tu-esposa>

# 5. Dejarla corriendo siempre, con respaldo diario
sudo cp deploy/biblioteca.service deploy/biblioteca-respaldo.service deploy/biblioteca-respaldo.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now biblioteca biblioteca-respaldo.timer
```

Los archivos de `deploy/` asumen que el usuario de la Pi es `pi` y que el código está en `/home/pi/biblioteca`. Si no es así, cambia `User=` y las rutas antes de copiarlos.

La app queda en el puerto **8001**. Desde cualquier equipo conectado a tu Tailscale entra a `http://<nombre-de-la-pi>:8001`. Si tu app financiera ya usa el 8001, cambia el puerto en `deploy/biblioteca.service`.

### La clave de la API de Claude

La lectura de lomos usa la API de Claude. La clave se crea en [console.anthropic.com](https://console.anthropic.com) (API Keys) y se cobra por uso, aparte de tu suscripción a Claude. Cada foto debería costar del orden de centavos de dólar. Puedes poner un límite de gasto mensual en la consola.

### La clave de Google Books

Cuando Open Library no tiene un libro (pasa mucho con ediciones en español), la app lo busca en Google Books. Sin clave, Google comparte una sola cuota diaria entre todas las apps del mundo que lo usan sin clave, y casi siempre está agotada. Con clave propia tienes 1000 búsquedas al día gratis:

1. Entra a [console.cloud.google.com](https://console.cloud.google.com) con tu cuenta de Google y crea un proyecto (por ejemplo "Biblioteca").
2. En **APIs y servicios → Biblioteca**, busca **Books API** y presiona **Habilitar**.
3. En **APIs y servicios → Credenciales**, presiona **Crear credenciales → Clave de API** y cópiala.
4. (Recomendado) En la clave, **Restringir clave → Restricciones de API → Books API**.
5. Pégala en `.env` como `GOOGLE_BOOKS_API_KEY=...` y reinicia: `sudo systemctl restart biblioteca`.

No pide tarjeta de crédito. Después de agregarla, en Importar usa "Volver a buscar" para que los libros que quedaron sin portada se busquen de nuevo.

### Actualizar

```bash
cd ~/biblioteca
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart biblioteca
```

### Respaldos

Cada día se guarda una copia de la base en `data/respaldos/` y se conservan las últimas 14. Las portadas sacadas con foto quedan en `data/portadas/` (no entran en el respaldo diario; si copias la carpeta `data` entera a otro lado, van incluidas). Para hacer una a mano: `.venv/bin/python manage.py respaldo`. Para restaurar, detén la app (`sudo systemctl stop biblioteca`), copia el respaldo sobre `data/biblioteca.db` y vuelve a iniciarla.

## Consejos para las fotos

- Una repisa por foto, de frente y con buena luz. Si se ven otras repisas, recorta la foto en la galería del celular y súbela con "Elegir de la galería".
- Lomos derechos. Si hay libros acostados, sácales otra foto aparte.
- Las lecturas dudosas aparecen en amarillo y vienen desmarcadas: revísalas antes de agregar.

## Desarrollo

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest
.venv/bin/uvicorn app.main:app --reload
```

Estructura: `app/main.py` (rutas), `app/vision.py` (lectura de lomos con Claude), `app/lookup.py` (Open Library y Google Books), `app/storygraph.py` (importación CSV), `app/db.py` (SQLite).
