"""Clínicas Odontológicas (Flask + HTML), con los datos en Neon (PostgreSQL en la nube).

Necesita:  pip install -r requirements.txt
Variables: DATABASE_URL (cadena de conexión de Neon), SECRET_KEY y, opcionales, CLAVE_INICIAL y SITIO
Ejecutar:  python prototipo.py   (crea las tablas si no existen)
"""
import base64
import io
import ipaddress
import json
import os
import secrets
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import psycopg
from flask import (Flask, Response, flash, g, redirect, render_template, request,
                   session, url_for)
from PIL import Image, ImageOps
from psycopg.rows import dict_row
from reportlab.graphics import renderSVG
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from werkzeug.security import check_password_hash, generate_password_hash

# Cuentas iniciales: dos por cada tipo y una de la Facultad. Se crean una sola vez en la base de datos
CUENTAS_INICIALES = {"00000001": ("Dra. Grace Mendoza", "profesor"),
                     "00000004": ("Dr. Gustavo Rivas", "profesor"),
                     "00000002": ("Grace Alvarado", "estudiante"),
                     "00000005": ("Gabriel Espinoza", "estudiante"),
                     "00000007": ("Gina Saavedra", "admin")}
CLAVE_INICIAL = os.environ.get("CLAVE_INICIAL", "uam2026")  # se guarda cifrada, nunca en texto plano
# Dirección pública del sistema: es la que va en el QR de los grupos, para que abra desde el celular del
# estudiante aunque la Facultad tenga el sistema abierto en otra dirección (por ejemplo, localhost)
SITIO = os.environ.get("SITIO", "https://clinicas-odontologicas-uam.fly.dev").rstrip("/")
ROLES = {"profesor": "Profesor", "estudiante": "Estudiante", "admin": "Facultad"}
# Pestanas de cada rol: (nombre, endpoint). La bitacora la revisan el profesor y la Facultad
MENUS = {"estudiante": [("Inicio", "menu"), ("Mis pacientes", "pacientes"), ("Mis citas", "citas"), ("Clínicas", "salas"),
                        ("Hoja de control", "control")],
         # El profesor solo revisa: no registra pacientes, citas ni tratamientos
         "profesor": [("Inicio", "menu"), ("Pacientes", "pacientes"), ("Citas", "citas"), ("Clínicas", "salas"),
                      ("Hoja de control", "control"), ("Bitácora", "bitacora")],
         "admin": [("Inicio", "menu"), ("Pacientes", "pacientes"), ("Citas", "citas"), ("Clínicas", "salas"),
                   ("Tarifario", "tarifario"), ("Inventario", "inventario"), ("Cuentas", "administrar_cuentas"),
                   ("Hoja de control", "control"), ("Bitácora", "bitacora")]}
QUE_HACE = {"estudiante": ["Registrar a sus pacientes y la razón por la que vienen",
                           "Agendar sus citas con la razón de cada cita",
                           "Subir la foto presencial, llenar el odontograma y pedir la revisión del profesor",
                           "Anotar el plan de tratamiento y marcar lo realizado",
                           "Recoger la firma del paciente en el consentimiento informado e imprimirlo",
                           "Subir la foto de cada recibo de pago y entregar impresa la hoja de control"],
            "profesor": ["Revisar y aprobar lo que registra cada estudiante",
                         "Aprobar el odontograma o devolverlo con lo que hay que corregir",
                         "Ver el historial de qué le hizo cada estudiante a cada paciente",
                         "Firmar los recibos de pago de la hoja de control de cada estudiante",
                         "Revisar la bitácora del sistema",
                         "No registra pacientes, citas ni tratamientos"],
            "admin": ["Crear las cuentas, ponerles contraseña y desactivarlas",
                      "Administrar las clínicas, el tarifario y el inventario",
                      "Registrar los abonos de los pacientes",
                      "Cambiar un paciente de estudiante archivando el trabajo del anterior",
                      "Revisar la bitácora del sistema"]}

# Odontograma: numeracion FDI de los 32 dientes permanentes, como se ven de frente al paciente
ARRIBA = [*range(18, 10, -1), *range(21, 29)]
ABAJO = [*range(48, 40, -1), *range(31, 39)]
DIENTES = set(ARRIBA + ABAJO)
ESTADOS = {"Sano": "#ffffff", "Caries": "#e57373", "Obturado": "#64b5f6", "Endodoncia": "#ba68c8",
           "Corona": "#ffd54f", "Extracción indicada": "#ffb74d", "Extraído": "#ffffff"}  # estado -> color
REVISIONES = {"": "Sin pedir", "pedida": "Pedida, esperando al profesor", "aprobada": "Aprobada",
              "devuelta": "Devuelta para corregir"}

TABLAS = """
CREATE TABLE IF NOT EXISTS cuentas (
    cif text PRIMARY KEY, nombre text NOT NULL, rol text NOT NULL, clave text NOT NULL);
CREATE TABLE IF NOT EXISTS salas (
    id serial PRIMARY KEY, nombre text NOT NULL UNIQUE, disponible boolean NOT NULL DEFAULT true);
CREATE TABLE IF NOT EXISTS pacientes (
    id serial PRIMARY KEY, nombre text NOT NULL, cedula text NOT NULL UNIQUE, telefono text NOT NULL DEFAULT '',
    razon text NOT NULL DEFAULT '', estudiante text REFERENCES cuentas, aprobado boolean NOT NULL DEFAULT false,
    firma text NOT NULL DEFAULT '', firma_fecha text);
CREATE TABLE IF NOT EXISTS trabajo (
    id serial PRIMARY KEY, paciente_id int NOT NULL REFERENCES pacientes ON DELETE CASCADE,
    estudiante text REFERENCES cuentas, desde text NOT NULL, hasta text, resumen text NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS citas (
    id serial PRIMARY KEY, paciente_id int NOT NULL REFERENCES pacientes ON DELETE CASCADE,
    fecha text NOT NULL, hora text NOT NULL, sala text NOT NULL DEFAULT '', razon text NOT NULL);
CREATE TABLE IF NOT EXISTS bitacora (
    id serial PRIMARY KEY, cuando timestamptz NOT NULL DEFAULT now(), cif text, nombre text, rol text, accion text);
ALTER TABLE bitacora ADD COLUMN IF NOT EXISTS lugar text NOT NULL DEFAULT '';
-- La tabla salas guarda las clinicas (unidades dentales); en_sala es el grupo: 1 pregrado, 2 posgrado
ALTER TABLE salas ADD COLUMN IF NOT EXISTS en_sala int NOT NULL DEFAULT 1;
ALTER TABLE cuentas ADD COLUMN IF NOT EXISTS fallos int NOT NULL DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS bloqueada_hasta timestamptz,
                    ADD COLUMN IF NOT EXISTS activa boolean NOT NULL DEFAULT true;
CREATE TABLE IF NOT EXISTS reportes (
    id serial PRIMARY KEY, sala_id int NOT NULL REFERENCES salas ON DELETE CASCADE, cif text REFERENCES cuentas,
    motivo text NOT NULL, cuando timestamptz NOT NULL DEFAULT now(),
    resuelto_por text REFERENCES cuentas, resuelto_cuando timestamptz);
-- Una clinica tiene a lo sumo una falla abierta: si ya la reportaron, la Facultad ya esta avisada
CREATE UNIQUE INDEX IF NOT EXISTS una_falla_abierta ON reportes (sala_id) WHERE resuelto_cuando IS NULL;
-- Odontograma: solo se guardan los dientes anotados; el que no esta es sano
ALTER TABLE pacientes ADD COLUMN IF NOT EXISTS odonto_revision text NOT NULL DEFAULT '',
                      ADD COLUMN IF NOT EXISTS odonto_revisor text REFERENCES cuentas,
                      ADD COLUMN IF NOT EXISTS odonto_nota text NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS dientes (
    paciente_id int NOT NULL REFERENCES pacientes ON DELETE CASCADE, diente int NOT NULL, estado text NOT NULL,
    nota text NOT NULL DEFAULT '', PRIMARY KEY (paciente_id, diente));
CREATE TABLE IF NOT EXISTS fotos (
    id serial PRIMARY KEY, paciente_id int NOT NULL REFERENCES pacientes ON DELETE CASCADE, cif text REFERENCES cuentas,
    cuando timestamptz NOT NULL DEFAULT now(), imagen bytea NOT NULL);
-- Tratamientos y cobros: el precio se copia del tarifario al anotar, asi un cambio de precio no altera lo anotado
CREATE TABLE IF NOT EXISTS tarifario (
    id serial PRIMARY KEY, nombre text NOT NULL UNIQUE, precio numeric(10, 2) NOT NULL CHECK (precio >= 0),
    activo boolean NOT NULL DEFAULT true);
CREATE TABLE IF NOT EXISTS tratamientos (
    id serial PRIMARY KEY, paciente_id int NOT NULL REFERENCES pacientes ON DELETE CASCADE,
    procedimiento_id int NOT NULL REFERENCES tarifario, diente int, precio numeric(10, 2) NOT NULL,
    estudiante text REFERENCES cuentas, anotado text NOT NULL, realizado text);
CREATE TABLE IF NOT EXISTS abonos (
    id serial PRIMARY KEY, paciente_id int NOT NULL REFERENCES pacientes ON DELETE CASCADE,
    monto numeric(10, 2) NOT NULL CHECK (monto > 0), cif text REFERENCES cuentas,
    cuando timestamptz NOT NULL DEFAULT now());
-- Alta del paciente (finalizado): la Facultad lo cierra y ya nadie mas cambia el expediente
ALTER TABLE pacientes ADD COLUMN IF NOT EXISTS finalizado text,
                      ADD COLUMN IF NOT EXISTS finalizado_por text REFERENCES cuentas,
                      ADD COLUMN IF NOT EXISTS finalizado_resumen text NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS materiales (
    id serial PRIMARY KEY, nombre text NOT NULL UNIQUE, unidad text NOT NULL DEFAULT '',
    cantidad int NOT NULL CHECK (cantidad >= 0), minimo int NOT NULL DEFAULT 0 CHECK (minimo >= 0));
-- Hoja de control: el numero del expediente fisico y los recibos de pago de cada tratamiento con su foto,
-- para que el numero se lea bien y el profesor lo firme sin copiar a mano numero tras numero
ALTER TABLE pacientes ADD COLUMN IF NOT EXISTS expediente text NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS recibos (
    id serial PRIMARY KEY, tratamiento_id int NOT NULL REFERENCES tratamientos ON DELETE CASCADE,
    numero text NOT NULL, fecha text NOT NULL, foto bytea NOT NULL, estudiante text REFERENCES cuentas,
    cuando timestamptz NOT NULL DEFAULT now(), firmado_por text REFERENCES cuentas, firmado text,
    UNIQUE (tratamiento_id, numero));
-- Grupos de estudiantes: la Facultad crea un codigo (va en un QR) y cada estudiante crea su cuenta con el.
-- El codigo vence, tiene cupo y se puede cerrar; cada cuenta recuerda con que grupo se registro
CREATE TABLE IF NOT EXISTS invitaciones (
    id serial PRIMARY KEY, nombre text NOT NULL, codigo text NOT NULL UNIQUE, cupo int NOT NULL CHECK (cupo > 0),
    vence timestamptz NOT NULL, cerrado boolean NOT NULL DEFAULT false, creado_por text REFERENCES cuentas,
    cuando timestamptz NOT NULL DEFAULT now());
ALTER TABLE cuentas ADD COLUMN IF NOT EXISTS invitacion_id int REFERENCES invitaciones;
"""

app = Flask(__name__)
# Sin SECRET_KEY las sesiones se pierden al reiniciar, pero nunca quedan firmadas con una clave conocida
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE="FLY_APP_NAME" in os.environ,
                  # Las computadoras de la clinica son compartidas: la sesion se cierra tras 30 min sin uso
                  PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
                  # la foto presencial de un celular pesa unos MB; se achica antes de guardarla
                  MAX_CONTENT_LENGTH=12_000_000)

FORMATO = "%d/%m/%Y"  # las fechas se muestran como dd/mm/aaaa; se eligen en el calendario
MANAGUA = timezone(timedelta(hours=-6))  # Nicaragua no cambia de horario; el servidor esta en UTC
GRUPOS = {1: "Clínicas pregrado", 2: "Clínicas posgrado"}  # valor de salas.en_sala
MOTIVO_MAXIMO = 500  # caracteres del motivo de una clinica que no funciona
RECIBO_MAXIMO = 40  # caracteres del numero de recibo
FALLOS_MAXIMOS, MINUTOS_BLOQUEO = 5, 15  # tras 5 claves equivocadas seguidas la cuenta espera 15 min
CLAVE_MINIMA = 8  # caracteres de la contraseña que la Facultad le pone a una cuenta
CUPO_MAXIMO, DIAS_MAXIMO = 200, 30  # de un grupo: cuántos estudiantes se registran con su QR y cuánto dura
CIF_MAXIMO, NOMBRE_MAXIMO = 20, 80  # caracteres de lo que escribe el estudiante al registrarse
FOTO_LADO = 1600  # pixeles del lado mas largo de la foto presencial guardada


def bd():
    """Una conexion a Neon por peticion; se cierra al terminar."""
    if "bd" not in g:
        g.bd = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row,
                               prepare_threshold=None)  # el pooler de Neon no admite sentencias preparadas
    return g.bd

@app.teardown_appcontext
def cerrar_bd(_):
    if (conexion := g.pop("bd", None)) is not None:
        conexion.close()

def sql(consulta, *args):
    return bd().execute(consulta, args)


def crear_tablas():
    with app.app_context():
        sql(TABLAS)
        for cif, (nombre, rol) in CUENTAS_INICIALES.items():
            sql("INSERT INTO cuentas VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                cif, nombre, rol, generate_password_hash(CLAVE_INICIAL))
        if not sql("SELECT 1 FROM salas").fetchone():
            for n in range(1, 21):  # las clinicas 1 a 10 son de pregrado y las 11 a 20 de posgrado
                sql("INSERT INTO salas (nombre, disponible, en_sala) VALUES (%s, %s, %s)",
                    f"Clínica {n}", n != 3, 1 if n <= 10 else 2)

@app.cli.command("crear-tablas")
def crear_tablas_cli():
    crear_tablas()


def hoy():
    return datetime.now(MANAGUA).date().isoformat()

def dmy(iso):
    """2026-10-28 -> 28/10/2026, el formato que se usa aqui."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime(FORMATO)
    except (ValueError, TypeError):
        return iso or ""

def error_de_cita(fecha, hora, sala):
    """Por qué no se puede agendar la cita; None si se puede. Fecha aaaa-mm-dd del calendario, hora HH:MM."""
    try:
        cuando = datetime.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M").replace(tzinfo=MANAGUA)
    except ValueError:
        return "Elija la fecha en el calendario y la hora de la cita."
    if cuando < datetime.now(MANAGUA):
        return "No se pueden agendar citas en una fecha u hora que ya pasó."
    if sala and sql("SELECT 1 FROM salas WHERE nombre = %s AND NOT disponible", sala).fetchone():
        return "Esa clínica no está disponible."
    if sala and sql("SELECT 1 FROM citas WHERE sala = %s AND fecha = %s AND hora = %s", sala, fecha, hora).fetchone():
        return "Esa clínica ya está ocupada a esa hora."
    return None

app.jinja_env.filters["dmy"] = dmy
app.jinja_env.globals["GRUPOS"] = GRUPOS

def monto(texto):
    """'1250.5' -> Decimal('1250.50'); None si no es un monto valido (cabe en numeric(10, 2))."""
    try:
        m = Decimal((texto or "").strip())
    except InvalidOperation:
        return None
    return m.quantize(Decimal("0.01")) if m.is_finite() and 0 <= m < 10**8 else None

def cordobas(m):
    return f"C$ {m:,.2f}"

app.jinja_env.filters["cordobas"] = cordobas

def foto_jpeg(archivo):
    """La foto subida, derecha, achicada y en JPEG; None si no es una imagen."""
    try:
        with Image.open(archivo) as imagen:
            imagen = ImageOps.exif_transpose(imagen).convert("RGB")  # el celular guarda la foto girada
            imagen.thumbnail((FOTO_LADO, FOTO_LADO))
            salida = io.BytesIO()
            imagen.save(salida, "JPEG", quality=85)
            return salida.getvalue()
    except (OSError, ValueError, AttributeError, Image.DecompressionBombError):  # sin archivo o no es imagen
        return None

def cuentas():
    """CIF -> (nombre, rol). Se lee una vez por peticion."""
    # ponytail: trae todas las cuentas cada vez; filtrar por CIF si llegan a ser miles
    if "cuentas" not in g:
        filas = sql("SELECT cif, nombre, rol, activa FROM cuentas").fetchall()
        g.cuentas = {f["cif"]: (f["nombre"], f["rol"]) for f in filas}
        g.inactivas = {f["cif"] for f in filas if not f["activa"]}  # siguen con nombre en el historial
    return g.cuentas

def quien():
    """CIF, rol y nombre de quien entro; None si no hay sesion o si la Facultad desactivo la cuenta."""
    cif = session.get("cif")
    return (cif, *cuentas()[cif]) if cif in cuentas() and cif not in g.inactivas else None

def ip_del_usuario():
    # En Fly la IP real llega en Fly-Client-IP; fuera de Fly ese encabezado lo podria inventar cualquiera
    return (request.headers.get("Fly-Client-IP") if "FLY_APP_NAME" in os.environ else None) or request.remote_addr or ""

def lugar_de(ip):
    """Ciudad, región y país aproximados de la IP (no la dirección exacta). Se busca una vez por sesión e IP."""
    guardado = session.get("lugar")
    if guardado and guardado[0] == ip:
        return guardado[1]
    try:
        # Una IP de la red local no dice dónde está: se usa la conexión a internet de esa misma red
        consulta = ip if ipaddress.ip_address(ip).is_global else ""
        with urllib.request.urlopen(f"https://ipwho.is/{consulta}?fields=success,city,region,country", timeout=3) as r:
            datos = json.load(r)
        partes = [datos[c] for c in ("city", "region", "country") if datos.get("success") and datos.get(c)]
        lugar = ", ".join(dict.fromkeys(partes))  # "Managua, Managua, Nicaragua" -> "Managua, Nicaragua"
    except (OSError, ValueError):  # sin respuesta del servicio o IP rara: la accion se anota igual
        lugar = ""
    session["lugar"] = [ip, lugar]
    return lugar

def anotar(accion, cif=None):
    """Bitacora: deja constancia de lo que acaba de hacer el usuario (o la cuenta indicada) y desde qué
    lugar aproximado. La IP solo se usa para saber el lugar; no se guarda."""
    cif, nombre, rol = (cif, *cuentas()[cif]) if cif else quien()
    sql("INSERT INTO bitacora (cif, nombre, rol, accion, lugar) VALUES (%s, %s, %s, %s, %s)",
        cif, nombre, ROLES[rol], accion, lugar_de(ip_del_usuario()))

def comun():
    """Datos que necesita la plantilla base en todas las paginas."""
    cif, nombre, rol = quien()
    avisos = {}
    if rol == "admin":  # la Facultad ve en la pestana cuantas clinicas fallan y cuantos materiales se acaban
        avisos = sql("SELECT (SELECT count(*) FROM reportes WHERE resuelto_cuando IS NULL) AS salas,"
                     " (SELECT count(*) FROM materiales WHERE cantidad <= minimo) AS inventario").fetchone()
    elif rol == "profesor":  # y el profesor, cuantos recibos esperan su firma
        avisos = sql("SELECT count(*) AS control FROM recibos WHERE firmado IS NULL").fetchone()
    pestanas = [(f"{t} ({avisos[e]})" if avisos.get(e) else t, e) for t, e in MENUS[rol]]
    return {"cif": cif, "nombre": nombre, "rol": ROLES[rol], "es_rol": rol, "pestanas": pestanas,
            "fallas": avisos.get("salas", 0), "bajos": avisos.get("inventario", 0)}

def paciente(pid):
    """El paciente, solo si el usuario puede verlo: cada estudiante ve unicamente los suyos.
    Si la Facultad le dio de alta, solo ella puede cambiar algo (por ejemplo, un abono)."""
    cif, _, rol = quien()
    p = sql("SELECT * FROM pacientes WHERE id = %s", pid).fetchone()
    if p and rol == "estudiante" and p["estudiante"] != cif:
        return None
    if p and p["finalizado"] and rol != "admin" and request.method == "POST":
        flash(f"{p['nombre']} ya recibió el alta: su expediente ya no se puede cambiar.", "error")
        return None
    return p

def mis_pacientes():
    # La firma solo importa para saber si ya firmo; no hace falta traer la imagen
    cif, _, rol = quien()
    return sql("SELECT id, nombre, cedula, telefono, razon, estudiante, aprobado, firma <> '' AS firma, finalizado"
               " FROM pacientes WHERE %s OR estudiante = %s ORDER BY id", rol != "estudiante", cif).fetchall()

def nombre_de(cif):
    return cuentas()[cif][0] if cif in cuentas() else "sin asignar"

def todas_las_salas():
    """Las clinicas, ordenadas por sala, y si tienen una falla sin solucionar, su motivo y quién la reportó."""
    return sql("SELECT s.*, r.id AS reporte_id, r.motivo, r.cif AS reporto,"
               " to_char(r.cuando AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS reportada"
               " FROM salas s LEFT JOIN reportes r ON r.sala_id = s.id AND r.resuelto_cuando IS NULL"
               " ORDER BY s.en_sala, s.id").fetchall()


@app.before_request
def exigir_sesion():
    if request.endpoint not in ("entrar", "registro", "static") and quien() is None:
        return redirect(url_for("entrar"))


@app.route("/", methods=["GET", "POST"])
def entrar():
    datos = {"cif": "", "error_cif": "", "error_clave": "", "aviso": ""}
    if request.method == "POST":
        cif = request.form.get("cif", "").strip()
        clave = request.form.get("clave", "")
        datos["cif"] = cif
        if not cif:
            datos["error_cif"] = "Escriba su CIF."
        if not clave:
            datos["error_clave"] = "Escriba su contraseña."
        if cif and clave:
            cuenta = sql("SELECT clave, activa, ceil(extract(epoch FROM bloqueada_hasta - now()) / 60) AS espera "
                         "FROM cuentas WHERE cif = %s", cif).fetchone()
            if cuenta and cuenta["espera"] and cuenta["espera"] > 0:
                # Mientras esta bloqueada ni se mira la clave: asi no se puede seguir probando
                datos["aviso"] = f"Cuenta bloqueada por intentos fallidos. Intente de nuevo en {cuenta['espera']:.0f} min."
            elif cuenta and check_password_hash(cuenta["clave"], clave) and not cuenta["activa"]:
                datos["aviso"] = "Esta cuenta está desactivada. Consulte con la Facultad."
            elif cuenta and check_password_hash(cuenta["clave"], clave):
                sql("UPDATE cuentas SET fallos = 0 WHERE cif = %s", cif)
                session.permanent = True  # cuenta los 30 min desde la ultima pagina que se abrio
                session["cif"] = cif
                anotar("Entró al sistema")
                return redirect(url_for("menu"))
            else:
                datos["aviso"] = "CIF o contraseña incorrectos."
                if cuenta and sql("UPDATE cuentas SET fallos = fallos + 1 WHERE cif = %s RETURNING fallos",
                                  cif).fetchone()["fallos"] >= FALLOS_MAXIMOS:
                    sql("UPDATE cuentas SET fallos = 0, bloqueada_hasta = now() + %s * interval '1 minute' "
                        "WHERE cif = %s", MINUTOS_BLOQUEO, cif)
                    anotar(f"Cuenta bloqueada {MINUTOS_BLOQUEO} min por {FALLOS_MAXIMOS} contraseñas equivocadas", cif)
                    datos["aviso"] = (f"Demasiados intentos fallidos. La cuenta queda bloqueada "
                                      f"{MINUTOS_BLOQUEO} min.")
    return render_template("entrar.html", **datos)


@app.route("/menu")
def menu():
    # Al profesor le aparecen los odontogramas que esperan su revision
    revisar = sql("SELECT id, nombre, estudiante FROM pacientes WHERE odonto_revision = 'pedida' ORDER BY id"
                  ).fetchall() if quien()[2] == "profesor" else []
    return render_template("menu.html", **comun(), que_hace=QUE_HACE[quien()[2]], revisar=revisar,
                           nombre_de=nombre_de)


@app.route("/pacientes", methods=["GET", "POST"])
def pacientes():
    cif, _, rol = quien()
    form = {}
    if request.method == "POST":
        accion = request.form.get("accion")
        if accion == "registrar" and rol == "estudiante":
            form = {c: request.form.get(c, "").strip() for c in ("nombre", "cedula", "telefono", "expediente")}
            if not form["nombre"] or not form["cedula"]:
                flash("El nombre y la cédula del paciente son obligatorios.", "error")
            elif sql("SELECT 1 FROM pacientes WHERE cedula = %s", form["cedula"]).fetchone():
                flash("Ya existe un paciente con esa cédula.", "error")
            else:
                with bd().transaction():
                    pid = sql("INSERT INTO pacientes (nombre, cedula, telefono, expediente, estudiante)"
                              " VALUES (%s, %s, %s, %s, %s) RETURNING id", form["nombre"], form["cedula"],
                              form["telefono"], form["expediente"], cif).fetchone()["id"]
                    sql("INSERT INTO trabajo (paciente_id, estudiante, desde) VALUES (%s, %s, %s)", pid, cif, hoy())
                    anotar(f"Registró al paciente {form['nombre']} ({form['cedula']})")
                # Paso siguiente: la razon por la que esta el paciente
                return redirect(url_for("razon", pid=pid))
        elif accion == "aprobar" and rol == "profesor":  # el profesor solo revisa
            p = paciente(request.form.get("id", type=int))
            if p:
                aprobado = request.form.get("aprobado") == "1"
                sql("UPDATE pacientes SET aprobado = %s WHERE id = %s", aprobado, p["id"])
                anotar(("Aprobó" if aprobado else "Retiró la aprobación de") + f" a {p['nombre']}")
                flash("Paciente aprobado." if aprobado else "Aprobación retirada.", "ok")
    return render_template("pacientes.html", **comun(), pacientes=mis_pacientes(), form=form, nombre_de=nombre_de)


@app.route("/pacientes/<int:pid>/razon", methods=["GET", "POST"])
def razon(pid):
    """Paso 2 del registro: por qué está el paciente y, si ya se sabe, su primera cita."""
    p = paciente(pid)
    if not p or quien()[2] != "estudiante":
        return redirect(url_for("pacientes"))
    if request.method == "POST":
        razon = request.form.get("razon", "").strip()
        fecha, hora, sala = (request.form.get(c, "").strip() for c in ("fecha", "hora", "sala"))
        # La primera cita es opcional, pero si se empieza a llenar se valida igual que en Citas
        error = (fecha or hora) and error_de_cita(fecha, hora, sala)
        p["razon"] = razon  # si hay error, el formulario vuelve con lo que se escribio
        if not razon:
            flash("Escriba la razón por la que viene el paciente.", "error")
        elif error:
            flash(error, "error")
        else:
            sql("UPDATE pacientes SET razon = %s WHERE id = %s", razon, pid)
            anotar(f"Anotó la razón de {p['nombre']}: {razon}")
            if fecha:
                sql("INSERT INTO citas (paciente_id, fecha, hora, sala, razon) VALUES (%s, %s, %s, %s, %s)",
                    pid, fecha, hora, sala, request.form.get("razon_cita", "").strip() or razon)
                anotar(f"Agendó cita de {p['nombre']} el {dmy(fecha)} a las {hora}")
                flash("Razón guardada y cita agendada.", "ok")
            else:
                flash("Razón de la consulta guardada.", "ok")
            return redirect(url_for("expediente", pid=pid))
    return render_template("razon.html", **comun(), p=p, hoy=hoy(), salas=todas_las_salas())


@app.route("/pacientes/<int:pid>/editar", methods=["GET", "POST"])
def editar(pid):
    """El estudiante a cargo corrige los datos del paciente si se equivocó al registrarlo."""
    p = paciente(pid)
    if not p or quien()[2] != "estudiante":
        return redirect(url_for("pacientes"))
    if request.method == "POST":
        form = {c: request.form.get(c, "").strip() for c in ("nombre", "cedula", "telefono", "expediente")}
        p.update(form)  # si hay error, el formulario vuelve con lo que se escribio
        if not form["nombre"] or not form["cedula"]:
            flash("El nombre y la cédula del paciente son obligatorios.", "error")
        elif sql("SELECT 1 FROM pacientes WHERE cedula = %s AND id <> %s", form["cedula"], pid).fetchone():
            flash("Ya existe otro paciente con esa cédula.", "error")
        else:
            sql("UPDATE pacientes SET nombre = %s, cedula = %s, telefono = %s, expediente = %s WHERE id = %s",
                form["nombre"], form["cedula"], form["telefono"], form["expediente"], pid)
            anotar(f"Corrigió los datos de {form['nombre']} ({form['cedula']})")
            flash("Datos del paciente guardados.", "ok")
            return redirect(url_for("expediente", pid=pid))
    return render_template("editar.html", **comun(), p=p)


@app.route("/pacientes/<int:pid>")
def expediente(pid):
    p = paciente(pid)
    if not p:
        return redirect(url_for("pacientes"))
    p["trabajo"] = sql("SELECT * FROM trabajo WHERE paciente_id = %s ORDER BY id", pid).fetchall()
    return render_template("expediente.html", **comun(), p=p, nombre_de=nombre_de, revisiones=REVISIONES,
                           citas=sql("SELECT * FROM citas WHERE paciente_id = %s", pid).fetchall())


@app.route("/pacientes/<int:pid>/traspaso", methods=["GET", "POST"])
def traspaso(pid):
    """Cambiar de estudiante: el trabajo del anterior se archiva con el resumen de lo que le hizo."""
    p = paciente(pid)
    if not p or quien()[2] != "admin" or p["finalizado"]:  # un paciente finalizado ya no cambia de estudiante
        return redirect(url_for("pacientes"))
    estudiantes = [(c, n) for c, (n, r) in cuentas().items() if r == "estudiante" and c not in g.inactivas]
    if request.method == "POST":
        nuevo = request.form.get("estudiante", "")
        resumen = request.form.get("resumen", "").strip()
        if nuevo not in dict(estudiantes) or nuevo == p["estudiante"]:
            flash("Elija el estudiante que recibe al paciente.", "error")
        elif not resumen:
            flash("Escriba qué se le hizo al paciente y qué trabajó el estudiante anterior.", "error")
        else:
            with bd().transaction():
                sql("UPDATE trabajo SET hasta = %s, resumen = %s WHERE paciente_id = %s AND hasta IS NULL",
                    hoy(), resumen, pid)
                sql("INSERT INTO trabajo (paciente_id, estudiante, desde) VALUES (%s, %s, %s)", pid, nuevo, hoy())
                sql("UPDATE pacientes SET estudiante = %s WHERE id = %s", nuevo, pid)
                anotar(f"Pasó a {p['nombre']} de {nombre_de(p['estudiante'])} a {nombre_de(nuevo)}")
            flash(f"{p['nombre']} pasó a {nombre_de(nuevo)}. El trabajo anterior quedó archivado.", "ok")
            return redirect(url_for("expediente", pid=pid))
    return render_template("traspaso.html", **comun(), p=p, estudiantes=estudiantes, nombre_de=nombre_de)


@app.route("/pacientes/<int:pid>/cerrar", methods=["GET", "POST"])
def cerrar(pid):
    """Solo la Facultad: da de alta al paciente, anula el alta o elimina al paciente."""
    p = paciente(pid)
    cif, _, rol = quien()
    if not p or rol != "admin":
        return redirect(url_for("pacientes"))
    if request.method == "POST":
        accion, texto = request.form.get("accion"), request.form.get("texto", "").strip()
        if accion == "finalizar" and not p["finalizado"]:
            if not texto:
                flash("Escriba cómo terminó el tratamiento del paciente.", "error")
            else:
                sql("UPDATE pacientes SET finalizado = %s, finalizado_por = %s, finalizado_resumen = %s WHERE id = %s",
                    hoy(), cif, texto, pid)
                anotar(f"Dio de alta a {p['nombre']}: {texto}")
                flash(f"{p['nombre']} recibió el alta. Su expediente ya no se puede cambiar.", "ok")
                return redirect(url_for("expediente", pid=pid))
        elif accion == "reabrir" and p["finalizado"]:
            sql("UPDATE pacientes SET finalizado = NULL, finalizado_por = NULL, finalizado_resumen = '' WHERE id = %s",
                pid)
            anotar(f"Anuló el alta de {p['nombre']}")
            flash(f"Se anuló el alta de {p['nombre']}.", "ok")
            return redirect(url_for("expediente", pid=pid))
        elif accion == "eliminar":
            # Se pide la cedula para que no se borre a nadie por un clic equivocado
            if request.form.get("cedula", "").strip() != p["cedula"] or not texto:
                flash("Para eliminarlo escriba la cédula del paciente y el motivo.", "error")
            elif any(r["firmado"] for r in recibos_de(paciente_id=pid)):
                flash(f"{p['nombre']} tiene recibos de pago firmados por el docente: no se puede eliminar."
                      " Si terminó su tratamiento, dele de alta.", "error")
            else:
                with bd().transaction():  # sus citas, odontograma, fotos, tratamientos, recibos y abonos se borran con el
                    sql("DELETE FROM pacientes WHERE id = %s", pid)
                    anotar(f"Eliminó al paciente {p['nombre']} ({p['cedula']}): {texto}")
                flash(f"{p['nombre']} fue eliminado con todo su expediente.", "ok")
                return redirect(url_for("pacientes"))
        return redirect(url_for("cerrar", pid=pid))
    return render_template("cerrar.html", **comun(), p=p, nombre_de=nombre_de, cuenta=cuenta_de(pid),
                           pendientes=[t for t in plan_de(pid) if not t["realizado"]])


@app.route("/pacientes/<int:pid>/consentimiento", methods=["GET", "POST"])
def consentimiento(pid):
    """Consentimiento informado del paciente, que firma con el dedo o el mouse. El docente no firma."""
    p = paciente(pid)
    if not p:
        return redirect(url_for("pacientes"))
    if request.method == "POST" and quien()[2] == "estudiante":
        firma = request.form.get("firma", "")
        if not firma.startswith("data:image/png;base64,"):
            flash("Falta la firma del paciente: dibújela con el dedo o el mouse.", "error")
        else:
            sql("UPDATE pacientes SET firma = %s, firma_fecha = %s WHERE id = %s", firma, hoy(), pid)
            anotar(f"Recogió la firma del consentimiento informado de {p['nombre']}")
            flash("Consentimiento informado firmado. Ya se puede imprimir.", "ok")
        return redirect(url_for("consentimiento", pid=pid))
    return render_template("consentimiento.html", **comun(), p=p, nombre_de=nombre_de)


@app.route("/citas", methods=["GET", "POST"])
def citas():
    cif, _, rol = quien()
    if request.method == "POST" and rol == "estudiante" and request.form.get("accion") == "cancelar":
        # Solo las citas que no han pasado: las pasadas son parte del historial del paciente
        cita = sql("SELECT * FROM citas WHERE id = %s AND fecha >= %s", request.form.get("id", type=int),
                   hoy()).fetchone()
        p = cita and paciente(cita["paciente_id"])
        if p:
            sql("DELETE FROM citas WHERE id = %s", cita["id"])
            anotar(f"Canceló la cita de {p['nombre']} del {dmy(cita['fecha'])} a las {cita['hora']}")
            flash("Cita cancelada.", "ok")
        return redirect(url_for("citas"))
    if request.method == "POST" and rol == "estudiante":
        p = paciente(request.form.get("paciente_id", type=int))
        f = {c: request.form.get(c, "").strip() for c in ("fecha", "hora", "sala", "razon")}
        if not (p and f["fecha"] and f["hora"] and f["razon"]):
            flash("Elige paciente, fecha, hora y la razón de la cita.", "error")
        elif error := error_de_cita(f["fecha"], f["hora"], f["sala"]):
            flash(error, "error")
        else:
            sql("INSERT INTO citas (paciente_id, fecha, hora, sala, razon) VALUES (%s, %s, %s, %s, %s)",
                p["id"], f["fecha"], f["hora"], f["sala"], f["razon"])
            anotar(f"Agendó cita de {p['nombre']} el {dmy(f['fecha'])} a las {f['hora']}")
            flash("Cita agendada.", "ok")
        return redirect(url_for("citas"))
    mios = {p["id"]: p for p in mis_pacientes()}
    return render_template("citas.html", **comun(), hoy=hoy(), salas=todas_las_salas(), pacientes=list(mios.values()),
                           citas=sql("SELECT * FROM citas WHERE paciente_id = ANY(%s)", list(mios)).fetchall(),
                           mios=mios, nombre_de=nombre_de)


@app.route("/salas", methods=["GET", "POST"])
def salas():
    """Plano de las salas: cada clínica con un check si está disponible o una X si no.
    El estudiante reporta las clínicas que no funcionan y la Facultad las marca como solucionadas."""
    cif, _, rol = quien()
    if request.method == "POST":
        accion = request.form.get("accion")
        if rol == "estudiante" and accion == "reportar":
            s = sql("SELECT id, nombre FROM salas WHERE id = %s", request.form.get("sala", type=int)).fetchone()
            motivo = request.form.get("motivo", "").strip()
            if not s or not motivo:
                flash("Elija la clínica y escriba qué es lo que no funciona.", "error")
            elif len(motivo) > MOTIVO_MAXIMO:
                flash(f"El motivo puede tener hasta {MOTIVO_MAXIMO} caracteres.", "error")
            else:
                with bd().transaction():
                    nuevo = sql("INSERT INTO reportes (sala_id, cif, motivo) VALUES (%s, %s, %s)"
                                " ON CONFLICT (sala_id) WHERE resuelto_cuando IS NULL DO NOTHING RETURNING id",
                                s["id"], cif, motivo).fetchone()
                    if nuevo:
                        sql("UPDATE salas SET disponible = false WHERE id = %s", s["id"])
                        anotar(f"Reportó que {s['nombre']} no funciona: {motivo}")
                if nuevo:
                    flash(f"{s['nombre']} quedó como no funcional. La Facultad fue notificada.", "ok")
                else:
                    flash(f"{s['nombre']} ya tiene una falla reportada; la Facultad ya está avisada.", "error")
        elif rol == "admin" and accion == "resolver":
            with bd().transaction():
                r = sql("UPDATE reportes SET resuelto_por = %s, resuelto_cuando = now()"
                        " WHERE id = %s AND resuelto_cuando IS NULL RETURNING sala_id, motivo",
                        cif, request.form.get("reporte", type=int)).fetchone()
                if r:
                    s = sql("UPDATE salas SET disponible = true WHERE id = %s RETURNING nombre", r["sala_id"]).fetchone()
                    anotar(f"Solucionó la falla de {s['nombre']}: {r['motivo']}")
            if r:
                flash(f"{s['nombre']} quedó solucionada y disponible otra vez.", "ok")
        elif rol == "admin" and accion == "agregar":
            nombre, en_sala = request.form.get("nombre", "").strip(), request.form.get("en_sala", type=int)
            if not nombre or en_sala not in GRUPOS:
                flash("Escriba el nombre de la clínica y elija si es de pregrado o de posgrado.", "error")
            elif sql("SELECT 1 FROM salas WHERE lower(nombre) = lower(%s)", nombre).fetchone():
                flash("Ya existe una clínica con ese nombre.", "error")
            else:
                sql("INSERT INTO salas (nombre, en_sala) VALUES (%s, %s)", nombre, en_sala)
                anotar(f"Agregó {nombre} en {GRUPOS[en_sala]}")
                flash(f"{nombre} agregada.", "ok")
        elif rol == "admin":
            s = sql("UPDATE salas SET disponible = NOT disponible WHERE id = %s RETURNING nombre, disponible",
                    request.form.get("sala", type=int)).fetchone()
            if s:
                anotar(f"Marcó {s['nombre']} como " + ("disponible" if s["disponible"] else "no disponible"))
        return redirect(url_for("salas"))
    return render_template("salas.html", **comun(), salas=todas_las_salas(), nombre_de=nombre_de,
                           motivo_maximo=MOTIVO_MAXIMO)


@app.route("/pacientes/<int:pid>/odontograma", methods=["GET", "POST"])
def odontograma(pid):
    """Foto presencial y odontograma digital. El estudiante a cargo los llena y pide la revisión;
    el profesor lo aprueba o lo devuelve con lo que hay que corregir."""
    p = paciente(pid)
    if not p:
        return redirect(url_for("pacientes"))
    cif, _, rol = quien()
    if request.method == "POST":
        accion = request.form.get("accion")
        if rol == "estudiante" and accion in ("diente", "foto"):
            if accion == "diente":
                d, estado = request.form.get("diente", type=int), request.form.get("estado")
                nota = request.form.get("nota", "").strip()
                if d not in DIENTES or estado not in ESTADOS:
                    flash("Elija el diente y su estado.", "error")
                    return redirect(url_for("odontograma", pid=pid))
                que = f"el diente {d}: {estado}" + (f" ({nota})" if nota else "")
                guardar = ("INSERT INTO dientes VALUES (%s, %s, %s, %s) ON CONFLICT (paciente_id, diente)"
                           " DO UPDATE SET estado = excluded.estado, nota = excluded.nota", pid, d, estado, nota)
            else:
                imagen = foto_jpeg(request.files.get("foto"))
                if not imagen:
                    flash("Elija una foto (JPG o PNG, o tómela con la cámara).", "error")
                    return redirect(url_for("odontograma", pid=pid))
                que = "una foto presencial"
                guardar = ("INSERT INTO fotos (paciente_id, cif, imagen) VALUES (%s, %s, %s)", pid, cif, imagen)
            with bd().transaction():
                sql(*guardar)
                # Si ya estaba aprobado y se cambia, el profesor lo tiene que volver a revisar
                sql("UPDATE pacientes SET odonto_revision = '' WHERE id = %s AND odonto_revision = 'aprobada'", pid)
                anotar(f"Anotó en el odontograma de {p['nombre']} {que}")
            flash("Foto guardada." if accion == "foto" else "Diente guardado.", "ok")
        elif rol == "estudiante" and accion == "pedir":
            if not sql("SELECT 1 FROM fotos WHERE paciente_id = %s", pid).fetchone():
                flash("Antes de pedir la revisión suba la foto presencial del paciente.", "error")
            elif sql("UPDATE pacientes SET odonto_revision = 'pedida' WHERE id = %s"
                     " AND odonto_revision IN ('', 'devuelta') RETURNING id", pid).fetchone():
                anotar(f"Pidió la revisión del odontograma de {p['nombre']}")
                flash("Revisión pedida. El profesor la verá en su Inicio.", "ok")
        elif rol == "profesor" and accion in ("aprobar", "devolver"):
            nota = request.form.get("nota", "").strip()
            if accion == "devolver" and not nota:
                flash("Escriba qué tiene que corregir el estudiante.", "error")
            elif sql("UPDATE pacientes SET odonto_revision = %s, odonto_revisor = %s, odonto_nota = %s"
                     " WHERE id = %s AND odonto_revision = 'pedida' RETURNING id",
                     "aprobada" if accion == "aprobar" else "devuelta", cif, nota, pid).fetchone():
                anotar(("Aprobó el odontograma de " if accion == "aprobar" else "Devolvió el odontograma de ")
                       + p["nombre"] + (f": {nota}" if nota else ""))
                flash("Odontograma aprobado." if accion == "aprobar" else "Odontograma devuelto al estudiante.", "ok")
        return redirect(url_for("odontograma", pid=pid))
    elegido = request.args.get("diente", type=int)
    return render_template(
        "odontograma.html", **comun(), p=p, nombre_de=nombre_de, arriba=ARRIBA, abajo=ABAJO, estados=ESTADOS,
        revisiones=REVISIONES,
        elegido=elegido if rol == "estudiante" and not p["finalizado"] and elegido in DIENTES else None,
        dientes={f["diente"]: f for f in sql("SELECT * FROM dientes WHERE paciente_id = %s", pid)},
        fotos=sql("SELECT id, cif, to_char(cuando AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS cuando"
                  " FROM fotos WHERE paciente_id = %s ORDER BY id DESC", pid).fetchall())


@app.route("/pacientes/<int:pid>/fotos/<int:fid>.jpg")
def foto(pid, fid):
    # Solo la ve quien puede ver al paciente
    f = paciente(pid) and sql("SELECT imagen FROM fotos WHERE id = %s AND paciente_id = %s", fid, pid).fetchone()
    if not f:
        return redirect(url_for("pacientes"))
    return Response(bytes(f["imagen"]), mimetype="image/jpeg")


def plan_de(pid):
    return sql("SELECT t.*, a.nombre FROM tratamientos t JOIN tarifario a ON a.id = t.procedimiento_id"
               " WHERE t.paciente_id = %s ORDER BY t.id", pid).fetchall()

def cuenta_de(pid):
    """Lo que cuesta el plan, lo realizado (lo que se cobra), lo abonado y el saldo pendiente."""
    c = sql("SELECT coalesce(sum(precio), 0) AS plan,"
            " coalesce(sum(precio) FILTER (WHERE realizado IS NOT NULL), 0) AS realizado,"
            " (SELECT coalesce(sum(monto), 0) FROM abonos WHERE paciente_id = %s) AS abonado"
            " FROM tratamientos WHERE paciente_id = %s", pid, pid).fetchone()
    c["saldo"] = c["realizado"] - c["abonado"]
    return c


@app.route("/pacientes/<int:pid>/tratamientos", methods=["GET", "POST"])
def tratamientos(pid):
    """Plan de tratamiento con los precios del tarifario y los abonos del paciente.
    El estudiante a cargo anota y marca lo realizado; la Facultad registra los abonos."""
    p = paciente(pid)
    if not p:
        return redirect(url_for("pacientes"))
    cif, _, rol = quien()
    if request.method == "POST":
        accion = request.form.get("accion")
        if rol == "estudiante" and accion == "anotar":
            proc = sql("SELECT * FROM tarifario WHERE id = %s AND activo",
                       request.form.get("procedimiento", type=int)).fetchone()
            diente = request.form.get("diente", type=int)  # opcional: hay procedimientos de toda la boca
            if not proc or (diente is not None and diente not in DIENTES):
                flash("Elija el procedimiento del tarifario.", "error")
            else:
                sql("INSERT INTO tratamientos (paciente_id, procedimiento_id, diente, precio, estudiante, anotado)"
                    " VALUES (%s, %s, %s, %s, %s, %s)", pid, proc["id"], diente, proc["precio"], cif, hoy())
                anotar(f"Anotó en el plan de {p['nombre']}: {proc['nombre']}" + (f" del {diente}" if diente else ""))
                flash("Tratamiento anotado en el plan.", "ok")
        elif rol == "estudiante" and accion in ("realizado", "quitar"):
            t = sql("SELECT t.id, a.nombre FROM tratamientos t JOIN tarifario a ON a.id = t.procedimiento_id"
                    " WHERE t.id = %s AND t.paciente_id = %s AND t.realizado IS NULL",
                    request.form.get("id", type=int), pid).fetchone()
            if t and accion == "realizado" and not p["firma"]:
                flash("Falta la firma del consentimiento informado: sin ella no se realiza ningún tratamiento.", "error")
            elif t and accion == "realizado":
                # queda a nombre de quien lo realizó: a ese estudiante le toca subir el recibo de pago
                sql("UPDATE tratamientos SET realizado = %s, estudiante = %s WHERE id = %s", hoy(), cif, t["id"])
                anotar(f"Realizó a {p['nombre']}: {t['nombre']}")
                flash(f"{t['nombre']} quedó como realizado.", "ok")
            elif t:  # lo pendiente se puede quitar; lo realizado ya es historial
                sql("DELETE FROM tratamientos WHERE id = %s", t["id"])
                anotar(f"Quitó del plan de {p['nombre']}: {t['nombre']}")
                flash(f"{t['nombre']} quitado del plan.", "ok")
        elif rol == "admin" and accion == "abono":
            m = monto(request.form.get("monto"))
            if m is None or m <= 0:
                flash("Escriba el monto del abono.", "error")
            else:
                sql("INSERT INTO abonos (paciente_id, monto, cif) VALUES (%s, %s, %s)", pid, m, cif)
                anotar(f"Registró un abono de {cordobas(m)} de {p['nombre']}")
                flash(f"Abono de {cordobas(m)} registrado.", "ok")
        return redirect(url_for("tratamientos", pid=pid))
    return render_template(
        "tratamientos.html", **comun(), p=p, nombre_de=nombre_de, plan=plan_de(pid), cuenta=cuenta_de(pid),
        recibos=recibos_de(paciente_id=pid),
        dientes=ARRIBA + ABAJO,
        tarifario=sql("SELECT * FROM tarifario WHERE activo ORDER BY nombre").fetchall(),
        abonos=sql("SELECT monto, cif, to_char(cuando AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS cuando"
                   " FROM abonos WHERE paciente_id = %s ORDER BY id", pid).fetchall())


@app.route("/tarifario", methods=["GET", "POST"])
def tarifario():
    """Los procedimientos y su precio. Lo lleva la Facultad; un procedimiento desactivado ya no se anota."""
    if quien()[2] != "admin":
        return redirect(url_for("menu"))
    if request.method == "POST":
        accion, precio = request.form.get("accion"), monto(request.form.get("precio"))
        if accion == "agregar":
            nombre = request.form.get("nombre", "").strip()
            if not nombre or precio is None:
                flash("Escriba el procedimiento y su precio.", "error")
            elif sql("SELECT 1 FROM tarifario WHERE lower(nombre) = lower(%s)", nombre).fetchone():
                flash("Ese procedimiento ya está en el tarifario.", "error")
            else:
                sql("INSERT INTO tarifario (nombre, precio) VALUES (%s, %s)", nombre, precio)
                anotar(f"Agregó al tarifario {nombre} a {cordobas(precio)}")
                flash(f"{nombre} agregado al tarifario.", "ok")
        elif accion == "precio":
            fila = precio is not None and sql("UPDATE tarifario SET precio = %s WHERE id = %s RETURNING nombre",
                                              precio, request.form.get("id", type=int)).fetchone()
            if fila:
                anotar(f"Cambió el precio de {fila['nombre']} a {cordobas(precio)}")
                flash(f"Precio de {fila['nombre']} cambiado. Lo ya anotado conserva su precio.", "ok")
            else:
                flash("Escriba el precio nuevo.", "error")
        elif accion == "activo":
            fila = sql("UPDATE tarifario SET activo = NOT activo WHERE id = %s RETURNING nombre, activo",
                       request.form.get("id", type=int)).fetchone()
            if fila:
                anotar(("Activó" if fila["activo"] else "Desactivó") + f" en el tarifario {fila['nombre']}")
        return redirect(url_for("tarifario"))
    return render_template("tarifario.html", **comun(),
                           procedimientos=sql("SELECT * FROM tarifario ORDER BY nombre").fetchall())


@app.route("/inventario", methods=["GET", "POST"])
def inventario():
    """Materiales de la clínica. La Facultad anota lo que entra y lo que sale y ve lo que se está acabando."""
    if quien()[2] != "admin":
        return redirect(url_for("menu"))
    if request.method == "POST":
        accion = request.form.get("accion")
        cantidad = request.form.get("cantidad", type=int)
        if accion == "agregar":
            nombre, unidad = (request.form.get(c, "").strip() for c in ("nombre", "unidad"))
            minimo = request.form.get("minimo", type=int)
            if not nombre or cantidad is None or minimo is None or cantidad < 0 or minimo < 0:
                flash("Escriba el material, cuántos hay y el mínimo para avisar.", "error")
            elif sql("SELECT 1 FROM materiales WHERE lower(nombre) = lower(%s)", nombre).fetchone():
                flash("Ese material ya está en el inventario.", "error")
            else:
                sql("INSERT INTO materiales (nombre, unidad, cantidad, minimo) VALUES (%s, %s, %s, %s)",
                    nombre, unidad, cantidad, minimo)
                anotar(f"Agregó al inventario {nombre}: {cantidad} {unidad}".rstrip())
                flash(f"{nombre} agregado al inventario.", "ok")
        elif accion in ("entrada", "salida"):
            if not cantidad or cantidad < 1:
                flash("Escriba cuántas unidades entran o salen.", "error")
            else:
                cambio = cantidad if accion == "entrada" else -cantidad
                # No sale lo que no hay: la cantidad nunca queda en negativo
                fila = sql("UPDATE materiales SET cantidad = cantidad + %s WHERE id = %s AND cantidad + %s >= 0"
                           " RETURNING nombre, unidad, cantidad", cambio, request.form.get("id", type=int),
                           cambio).fetchone()
                if fila:
                    anotar(f"{'Entrada' if cambio > 0 else 'Salida'} de {cantidad} {fila['unidad']} de"
                           f" {fila['nombre']}; quedan {fila['cantidad']}")
                    flash(f"{fila['nombre']}: quedan {fila['cantidad']} {fila['unidad']}.", "ok")
                else:
                    flash("No hay tantas unidades en el inventario.", "error")
        return redirect(url_for("inventario"))
    return render_template("inventario.html", **comun(), materiales=sql(
        "SELECT *, cantidad <= minimo AS bajo FROM materiales ORDER BY cantidad <= minimo DESC, nombre").fetchall())


@app.route("/cuentas", methods=["GET", "POST"])
def administrar_cuentas():
    """La Facultad crea las cuentas, les pone contraseña y las desactiva. La contraseña se guarda cifrada."""
    yo, _, rol = quien()
    if rol != "admin":
        return redirect(url_for("menu"))
    if request.method == "POST":
        accion, cif, clave = (request.form.get("accion"), request.form.get("cif", "").strip(),
                              request.form.get("clave", ""))
        if accion == "crear":
            nombre, rol_nuevo = request.form.get("nombre", "").strip(), request.form.get("rol")
            if not cif.isalnum() or not nombre or rol_nuevo not in ROLES:
                flash("Escriba el CIF (solo números y letras), el nombre y el rol.", "error")
            elif len(clave) < CLAVE_MINIMA:
                flash(f"La contraseña debe tener al menos {CLAVE_MINIMA} caracteres.", "error")
            elif cif in cuentas():
                flash("Ya existe una cuenta con ese CIF.", "error")
            else:
                sql("INSERT INTO cuentas (cif, nombre, rol, clave) VALUES (%s, %s, %s, %s)",
                    cif, nombre, rol_nuevo, generate_password_hash(clave))
                anotar(f"Creó la cuenta de {nombre} ({cif}, {ROLES[rol_nuevo]})")
                flash(f"Cuenta de {nombre} creada. Dele la contraseña en privado.", "ok")
        elif accion == "grupo":
            nombre = request.form.get("nombre", "").strip()
            cupo, dias = request.form.get("cupo", type=int), request.form.get("dias", type=int)
            if (not nombre or len(nombre) > NOMBRE_MAXIMO or not cupo or not dias
                    or not 1 <= cupo <= CUPO_MAXIMO or not 1 <= dias <= DIAS_MAXIMO):
                flash(f"Escriba el nombre del grupo, cuántos estudiantes son (hasta {CUPO_MAXIMO}) y cuántos días"
                      f" queda abierto el registro (hasta {DIAS_MAXIMO}).", "error")
            else:
                gid = sql("INSERT INTO invitaciones (nombre, codigo, cupo, vence, creado_por)"
                          " VALUES (%s, %s, %s, now() + %s * interval '1 day', %s) RETURNING id",
                          nombre, secrets.token_urlsafe(12), cupo, dias, yo).fetchone()["id"]
                anotar(f"Creó el grupo de estudiantes {nombre} (cupo {cupo}, registro abierto {dias} días)")
                return redirect(url_for("grupo", gid=gid))
        elif accion == "clave":
            if len(clave) < CLAVE_MINIMA:
                flash(f"La contraseña debe tener al menos {CLAVE_MINIMA} caracteres.", "error")
            elif sql("UPDATE cuentas SET clave = %s, fallos = 0, bloqueada_hasta = NULL WHERE cif = %s RETURNING cif",
                     generate_password_hash(clave), cif).fetchone():
                anotar(f"Le puso una contraseña nueva a {nombre_de(cif)} ({cif})")
                flash(f"Contraseña de {nombre_de(cif)} cambiada; si estaba bloqueada, ya puede entrar.", "ok")
        elif accion == "activa":
            if cif == yo:
                flash("No puede desactivar su propia cuenta.", "error")
            elif fila := sql("UPDATE cuentas SET activa = NOT activa WHERE cif = %s RETURNING nombre, activa",
                             cif).fetchone():
                anotar(("Activó" if fila["activa"] else "Desactivó") + f" la cuenta de {fila['nombre']} ({cif})")
                flash(f"Cuenta de {fila['nombre']} " + ("activada." if fila["activa"] else "desactivada."), "ok")
        return redirect(url_for("administrar_cuentas"))
    return render_template("cuentas.html", **comun(), roles=ROLES, clave_minima=CLAVE_MINIMA,
                           cupo_maximo=CUPO_MAXIMO, dias_maximo=DIAS_MAXIMO,
                           grupos=sql(INVITACIONES + " ORDER BY i.id DESC").fetchall(), lista=sql(
        "SELECT cif, nombre, rol, activa, CASE WHEN bloqueada_hasta > now() THEN"
        " to_char(bloqueada_hasta AT TIME ZONE 'America/Managua', 'HH24:MI') END AS bloqueada"
        " FROM cuentas ORDER BY activa DESC, rol, nombre").fetchall())


# Cada grupo con cuántos se han registrado y si su código todavía sirve
INVITACIONES = ("SELECT i.*, to_char(i.vence AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS hasta, n.registrados,"
                " CASE WHEN i.cerrado THEN 'Cerrado' WHEN i.vence <= now() THEN 'Vencido'"
                " WHEN n.registrados >= i.cupo THEN 'Lleno' ELSE 'Abierto' END AS estado FROM invitaciones i,"
                " LATERAL (SELECT count(*) AS registrados FROM cuentas WHERE invitacion_id = i.id) n")

def qr(direccion):
    """El código QR de una dirección, como SVG listo para el src de un <img>."""
    dibujo = Drawing(120, 120)
    dibujo.add(QrCodeWidget(direccion, barWidth=120, barHeight=120, barLevel="M"))
    return "data:image/svg+xml;base64," + base64.b64encode(renderSVG.drawToString(dibujo).encode()).decode()


@app.route("/cuentas/grupos/<int:gid>", methods=["GET", "POST"])
def grupo(gid):
    """El código QR del grupo, para proyectarlo o imprimirlo, y quiénes se han registrado con él."""
    if quien()[2] != "admin":
        return redirect(url_for("menu"))
    if request.method == "POST":  # cerrar el registro: el código deja de servir
        cerrado = sql("UPDATE invitaciones SET cerrado = true WHERE id = %s AND NOT cerrado RETURNING nombre",
                      gid).fetchone()
        if cerrado:
            anotar(f"Cerró el registro del grupo {cerrado['nombre']}")
            flash("Registro cerrado: el código ya no sirve.", "ok")
        return redirect(url_for("grupo", gid=gid))
    invitacion = sql(INVITACIONES + " WHERE i.id = %s", gid).fetchone()
    if not invitacion:
        return redirect(url_for("administrar_cuentas"))
    direccion = SITIO + url_for("registro", codigo=invitacion["codigo"])
    return render_template(
        "grupo.html", **comun(), grupo=invitacion, direccion=direccion, qr=qr(direccion), clave_minima=CLAVE_MINIMA,
        registrados=sql("SELECT cif, nombre, activa FROM cuentas WHERE invitacion_id = %s ORDER BY nombre",
                        gid).fetchall())


@app.route("/registro/<codigo>", methods=["GET", "POST"])
def registro(codigo):
    """Página a la que lleva el QR de un grupo: el estudiante escribe su CIF y su nombre y crea su contraseña.
    No pide sesión; la protege el código del grupo, que vence, tiene cupo y la Facultad puede cerrar.
    Solo crea cuentas de estudiante, y un estudiante nuevo no ve a ningún paciente."""
    invitacion = sql(INVITACIONES + " WHERE i.codigo = %s", codigo).fetchone()
    datos = {"cif": "", "nombre": "", "errores": {}}
    if request.method == "POST" and invitacion and invitacion["estado"] == "Abierto":
        cif, nombre = (request.form.get(c, "").strip() for c in ("cif", "nombre"))
        clave, errores = request.form.get("clave", ""), datos["errores"]
        datos.update(cif=cif, nombre=nombre)
        ya_existe = "Ya existe una cuenta con ese CIF. Si es suya, entre con su contraseña; si no, avise a la Facultad."
        if not cif.isalnum() or len(cif) > CIF_MAXIMO:
            errores["cif"] = "Escriba su CIF, solo con números y letras."
        elif cif in cuentas():
            errores["cif"] = ya_existe
        if not nombre or len(nombre) > NOMBRE_MAXIMO:
            errores["nombre"] = "Escriba su nombre completo."
        if len(clave) < CLAVE_MINIMA:
            errores["clave"] = f"La contraseña debe tener al menos {CLAVE_MINIMA} caracteres."
        elif clave != request.form.get("clave2", ""):
            errores["clave2"] = "Las dos contraseñas no son iguales."
        if not errores:
            cifrada = generate_password_hash(clave)
            with bd().transaction():
                # FOR UPDATE: si dos se registran a la vez por el último cupo, el segundo espera y ya lo ve lleno
                sql("SELECT 1 FROM invitaciones WHERE id = %s FOR UPDATE", invitacion["id"])
                invitacion = sql(INVITACIONES + " WHERE i.id = %s", invitacion["id"]).fetchone()
                creada = invitacion["estado"] == "Abierto" and sql(
                    "INSERT INTO cuentas (cif, nombre, rol, clave, invitacion_id) VALUES (%s, %s, 'estudiante', %s, %s)"
                    " ON CONFLICT DO NOTHING RETURNING cif", cif, nombre, cifrada, invitacion["id"]).fetchone()
            if creada:
                g.pop("cuentas", None)  # para que la sesión ya encuentre la cuenta nueva
                session.clear()
                session.permanent = True
                session["cif"] = cif
                anotar(f"Creó su cuenta con el código del grupo {invitacion['nombre']}")
                flash("Cuenta creada. La próxima vez entre con su CIF y la contraseña que acaba de crear.", "ok")
                return redirect(url_for("menu"))
            if invitacion["estado"] == "Abierto":  # otro la creó con ese CIF en el mismo instante
                errores["cif"] = ya_existe
    return render_template("registro.html", **datos, grupo=invitacion, clave_minima=CLAVE_MINIMA,
                           cif_maximo=CIF_MAXIMO, nombre_maximo=NOMBRE_MAXIMO)


def recibos_de(estudiante=None, paciente_id=None):
    """Los recibos de la hoja de control con su paciente y tratamiento; de un estudiante o de un paciente."""
    return sql("SELECT r.id, r.numero, r.fecha, r.estudiante, r.firmado, r.firmado_por, p.id AS pid,"
               " p.nombre AS paciente, p.expediente, a.nombre || coalesce(' del ' || t.diente, '') AS tratamiento"
               " FROM recibos r JOIN tratamientos t ON t.id = r.tratamiento_id"
               " JOIN pacientes p ON p.id = t.paciente_id JOIN tarifario a ON a.id = t.procedimiento_id"
               " WHERE (%s::text IS NULL OR r.estudiante = %s) AND (%s::int IS NULL OR p.id = %s)"
               " ORDER BY r.fecha, r.id", estudiante, estudiante, paciente_id, paciente_id).fetchall()


@app.route("/control", methods=["GET", "POST"])
def control():
    """Hoja de control de expediente y recibos de pago de tratamiento. El estudiante sube la foto de cada
    recibo con su numero, fecha y tratamiento; el profesor firma cada uno; al final del curso se imprime."""
    cif, _, rol = quien()
    if request.method == "POST":
        accion = request.form.get("accion")
        if rol == "estudiante" and accion == "subir":
            # Solo de un tratamiento que él mismo realizó; el paciente puede haber pasado a otro estudiante o
            # estar de alta: el recibo a veces llega después y la hoja de control no es parte del expediente
            t = sql("SELECT t.id, a.nombre, p.nombre AS paciente FROM tratamientos t"
                    " JOIN tarifario a ON a.id = t.procedimiento_id JOIN pacientes p ON p.id = t.paciente_id"
                    " WHERE t.id = %s AND t.estudiante = %s AND t.realizado IS NOT NULL",
                    request.form.get("tratamiento", type=int), cif).fetchone()
            numero = request.form.get("numero", "").strip()
            fecha = request.form.get("fecha", "").strip()
            try:
                fecha_ok = datetime.strptime(fecha, "%Y-%m-%d").date().isoformat() <= hoy()
            except ValueError:
                fecha_ok = False
            imagen = foto_jpeg(request.files.get("foto"))
            if not t:
                flash("Elija el paciente y el tratamiento realizado que se pagó.", "error")
            elif not numero or len(numero) > RECIBO_MAXIMO:
                flash(f"Escriba el número del recibo (hasta {RECIBO_MAXIMO} caracteres).", "error")
            elif not fecha_ok:
                flash("Elija en el calendario la fecha del recibo; no puede ser una fecha que no ha llegado.", "error")
            elif not imagen:
                flash("Suba la foto del recibo (JPG o PNG, o tómela con la cámara).", "error")
            elif sql("INSERT INTO recibos (tratamiento_id, numero, fecha, foto, estudiante) VALUES (%s, %s, %s, %s, %s)"
                     " ON CONFLICT DO NOTHING RETURNING id", t["id"], numero, fecha, imagen, cif).fetchone():
                anotar(f"Subió el recibo {numero} de {t['paciente']}: {t['nombre']}")
                flash(f"Recibo {numero} agregado a la hoja de control.", "ok")
            else:
                flash("Ese recibo ya está anotado para ese tratamiento.", "error")
        elif rol == "estudiante" and accion == "quitar":
            # Lo firmado por el profesor ya no se quita
            r = sql("DELETE FROM recibos WHERE id = %s AND estudiante = %s AND firmado IS NULL RETURNING numero",
                    request.form.get("id", type=int), cif).fetchone()
            if r:
                anotar(f"Quitó el recibo {r['numero']} de su hoja de control")
                flash(f"Recibo {r['numero']} quitado.", "ok")
        elif rol == "profesor" and accion in ("firmar", "quitar_firma"):
            if accion == "firmar":
                r = sql("UPDATE recibos SET firmado_por = %s, firmado = %s WHERE id = %s AND firmado IS NULL"
                        " RETURNING numero, estudiante", cif, hoy(), request.form.get("id", type=int)).fetchone()
            else:  # solo el profesor que firmó puede quitar su firma
                r = sql("UPDATE recibos SET firmado_por = NULL, firmado = NULL WHERE id = %s AND firmado_por = %s"
                        " RETURNING numero, estudiante", request.form.get("id", type=int), cif).fetchone()
            if r:
                anotar(("Firmó" if accion == "firmar" else "Quitó su firma de")
                       + f" el recibo {r['numero']} de {nombre_de(r['estudiante'])}")
                flash(f"Recibo {r['numero']} " + ("firmado." if accion == "firmar" else "sin firma otra vez."), "ok")
        return redirect(url_for("control", estudiante=request.args.get("estudiante") or None))
    estudiantes = sorted(((c, n) for c, (n, r) in cuentas().items() if r == "estudiante"), key=lambda e: e[1])
    elegido = cif if rol == "estudiante" else request.args.get("estudiante") or None
    if elegido not in dict(estudiantes):
        elegido = None
    pendientes = []  # los tratamientos que él realizó, para elegir a cuál corresponde el recibo
    if rol == "estudiante":
        pendientes = sql("SELECT t.id, p.nombre AS paciente, a.nombre || coalesce(' del ' || t.diente, '') AS tratamiento"
                         " FROM tratamientos t JOIN pacientes p ON p.id = t.paciente_id"
                         " JOIN tarifario a ON a.id = t.procedimiento_id"
                         " WHERE t.estudiante = %s AND t.realizado IS NOT NULL ORDER BY p.nombre, t.id", cif).fetchall()
    return render_template("control.html", **comun(), recibos=recibos_de(estudiante=elegido), elegido=elegido,
                           estudiantes=estudiantes, pendientes=pendientes, hoy=hoy(), nombre_de=nombre_de,
                           recibo_maximo=RECIBO_MAXIMO)


@app.route("/control/recibos/<int:rid>.jpg")
def recibo_foto(rid):
    # El estudiante ve solo sus recibos; el profesor y la Facultad, todos
    cif, _, rol = quien()
    r = sql("SELECT foto FROM recibos WHERE id = %s AND (%s OR estudiante = %s)", rid, rol != "estudiante",
            cif).fetchone()
    if not r:
        return redirect(url_for("control"))
    return Response(bytes(r["foto"]), mimetype="image/jpeg")


@app.route("/bitacora")
def bitacora():
    """Quién hizo qué y a qué hora. La revisan el profesor y la Facultad."""
    if quien()[2] == "estudiante":
        return redirect(url_for("menu"))
    # ponytail: solo las ultimas 500 lineas; paginar si hace falta revisar mas atras
    lineas = sql("SELECT to_char(cuando AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS cuando,"
                 " cif, nombre, rol, accion, lugar FROM bitacora ORDER BY id DESC LIMIT 500").fetchall()
    return render_template("bitacora.html", **comun(), lineas=lineas)


def firma_png(p):
    """La firma dibujada, lista para ponerla en el PDF; None si no hay."""
    if not p.get("firma", "").startswith("data:image/png;base64,"):
        return None
    try:
        return ImageReader(io.BytesIO(base64.b64decode(p["firma"].split(",", 1)[1])))
    except ValueError:  # firma recortada o mal formada
        return None


@app.route("/pacientes/<int:pid>/expediente.pdf")
def expediente_pdf(pid):
    # El estudiante a cargo, el profesor que revisa y la Facultad bajan el expediente en PDF
    p = paciente(pid)
    if not p:
        return redirect(url_for("pacientes"))
    p["trabajo"] = sql("SELECT * FROM trabajo WHERE paciente_id = %s ORDER BY id", pid).fetchall()
    archivo = io.BytesIO()
    hoja = canvas.Canvas(archivo, pagesize=letter)
    _, alto = letter
    y = alto - 60

    def linea(texto, fuente="Helvetica", tamano=11, salto=16):
        nonlocal y
        if y < 90:  # ya cabe poco: pasa a la hoja siguiente
            hoja.showPage()
            y = alto - 60
        hoja.setFont(fuente, tamano)
        hoja.drawString(50, y, texto)
        y -= salto

    linea("Expediente clínico", "Helvetica-Bold", 16, 20)
    linea(f"Clínicas Odontológicas · Facultad de Odontología UAM · {dmy(hoy())}", "Helvetica", 9, 28)
    for etiqueta, valor in (("Nº de paciente", p["id"]), ("Nº de expediente", p["expediente"] or "—"),
                            ("Nombre", p["nombre"]), ("Cédula", p["cedula"]), ("Teléfono", p["telefono"] or "—"),
                            ("Razón de la consulta", p["razon"] or "—"),
                            ("Estudiante a cargo", nombre_de(p["estudiante"])),
                            ("Estado", "Aprobado" if p["aprobado"] else "Pendiente de aprobación")):
        linea(f"{etiqueta}: {valor}")
    if p["finalizado"]:
        linea(f"Recibió el alta el {dmy(p['finalizado'])} por {nombre_de(p['finalizado_por'])}: "
              f"{p['finalizado_resumen']}")

    y -= 10
    linea("Trabajo por estudiante", "Helvetica-Bold", 12)
    for t in p["trabajo"]:
        linea(f"- {nombre_de(t['estudiante'])}: {dmy(t['desde'])} a {dmy(t['hasta']) if t['hasta'] else 'hoy'}."
              f" {t['resumen'] or 'sin resumen'}", "Helvetica", 10)

    agenda = sql("SELECT * FROM citas WHERE paciente_id = %s ORDER BY fecha, hora", pid).fetchall()
    if agenda:
        y -= 10
        linea("Citas", "Helvetica-Bold", 12)
        for c in agenda:
            linea(f"- {dmy(c['fecha'])} {c['hora']} · {c['razon']} · {c['sala'] or 'sin clínica'}", "Helvetica", 10)

    y -= 10
    linea("Odontograma", "Helvetica-Bold", 12)
    linea(f"Revisión del profesor: {REVISIONES[p['odonto_revision']]}"
          + (f" ({nombre_de(p['odonto_revisor'])})" if p["odonto_revision"] in ("aprobada", "devuelta") else ""),
          "Helvetica", 10)
    hallazgos = sql("SELECT * FROM dientes WHERE paciente_id = %s AND estado <> 'Sano' ORDER BY diente", pid).fetchall()
    for d in hallazgos:
        linea(f"- Diente {d['diente']}: {d['estado']}" + (f" · {d['nota']}" if d["nota"] else ""), "Helvetica", 10)
    if not hallazgos:
        linea("Sin hallazgos anotados: todos los dientes sanos.", "Helvetica", 10)

    y -= 10
    linea("Tratamientos y cobros", "Helvetica-Bold", 12)
    for t in plan_de(pid):
        linea(f"- {t['nombre']}" + (f" del {t['diente']}" if t["diente"] else "") + f" · {cordobas(t['precio'])} · "
              + (f"realizado el {dmy(t['realizado'])}" if t["realizado"] else "pendiente"), "Helvetica", 10)
    c = cuenta_de(pid)
    linea(f"Realizado: {cordobas(c['realizado'])} · Abonado: {cordobas(c['abonado'])} · "
          f"Saldo pendiente: {cordobas(c['saldo'])}", "Helvetica", 10)
    for r in recibos_de(paciente_id=pid):
        linea(f"- Recibo {r['numero']} · {dmy(r['fecha'])} · {r['tratamiento']} · "
              + (f"firmado por {nombre_de(r['firmado_por'])}" if r["firmado"] else "sin firma del docente"),
              "Helvetica", 10)

    y -= 14
    linea("Consentimiento informado", "Helvetica-Bold", 12)
    firma = firma_png(p)
    if y < 170 and firma:
        hoja.showPage()
        y = alto - 60
    try:
        if not firma:
            raise OSError
        hoja.drawImage(firma, 50, y - 70, width=200, height=70, mask="auto",
                       preserveAspectRatio=True, anchor="sw")
    except OSError:  # sin firma o con una imagen que no se puede leer
        linea("Sin firmar.", "Helvetica", 10)
    else:
        y -= 76
        hoja.line(50, y, 250, y)
        y -= 14
        linea(f"Firma del paciente · {dmy(p.get('firma_fecha'))}", "Helvetica", 9)

    hoja.showPage()
    hoja.save()
    return Response(archivo.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename=expediente_{p['cedula']}.pdf"})


@app.route("/salir")
def salir():
    session.clear()
    return redirect(url_for("entrar"))


if __name__ == "__main__":
    crear_tablas()
    print("\n  Clínicas Odontológicas:  http://localhost:5001\n")
    app.run(port=5001)
