"""Clínicas Odontológicas (Flask + HTML), con los datos en Neon (PostgreSQL en la nube).

Necesita:  pip install -r requirements.txt
Variables: DATABASE_URL (cadena de conexión de Neon), SECRET_KEY y, opcional, CLAVE_INICIAL
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

import psycopg
from flask import (Flask, Response, flash, g, redirect, render_template, request,
                   session, url_for)
from psycopg.rows import dict_row
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
ROLES = {"profesor": "Profesor", "estudiante": "Estudiante", "admin": "Facultad"}
# Pestanas de cada rol: (nombre, endpoint). La bitacora la revisan el profesor y la Facultad
MENUS = {"estudiante": [("Inicio", "menu"), ("Mis pacientes", "pacientes"), ("Mis citas", "citas"), ("Salas", "salas")],
         # El profesor solo revisa: no registra pacientes, citas ni tratamientos
         "profesor": [("Inicio", "menu"), ("Pacientes", "pacientes"), ("Citas", "citas"), ("Salas", "salas"),
                      ("Bitácora", "bitacora")],
         "admin": [("Inicio", "menu"), ("Pacientes", "pacientes"), ("Citas", "citas"), ("Salas", "salas"),
                   ("Bitácora", "bitacora")]}
QUE_HACE = {"estudiante": ["Registrar a sus pacientes y la razón por la que vienen",
                           "Agendar sus citas con la razón de cada cita",
                           "Llenar el odontograma y pedir la revisión del profesor",
                           "Recoger la firma del paciente en el consentimiento e imprimirlo"],
            "profesor": ["Revisar y aprobar lo que registra cada estudiante",
                         "Ver el historial de qué le hizo cada estudiante a cada paciente",
                         "Revisar la bitácora del sistema",
                         "No registra pacientes, citas ni tratamientos"],
            "admin": ["Crear las cuentas y administrar las salas",
                      "Cambiar un paciente de estudiante archivando el trabajo del anterior",
                      "Revisar la bitácora del sistema"]}
EN_PROCESO = {"Odontograma": "Foto presencial y odontograma digital",
              "Tratamientos y cobros": "Procedimientos, tarifario y abonos",
              "Inventario": "Materiales de la clínica"}

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
ALTER TABLE bitacora ADD COLUMN IF NOT EXISTS ip text NOT NULL DEFAULT '',
                     ADD COLUMN IF NOT EXISTS lugar text NOT NULL DEFAULT '';
"""

app = Flask(__name__)
# Sin SECRET_KEY las sesiones se pierden al reiniciar, pero nunca quedan firmadas con una clave conocida
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE="FLY_APP_NAME" in os.environ,
                  # Las computadoras de la clinica son compartidas: la sesion se cierra tras 30 min sin uso
                  PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
                  MAX_CONTENT_LENGTH=2_000_000)  # la firma dibujada pesa unas decenas de KB

FORMATO = "%d/%m/%Y"  # las fechas se muestran como dd/mm/aaaa; se eligen en el calendario
MANAGUA = timezone(timedelta(hours=-6))  # Nicaragua no cambia de horario; el servidor esta en UTC


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
            for nombre, disponible in (("Sala 1", True), ("Sala 2", True), ("Sala 3", False)):
                sql("INSERT INTO salas (nombre, disponible) VALUES (%s, %s)", nombre, disponible)

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
    if sala and sql("SELECT 1 FROM citas WHERE sala = %s AND fecha = %s AND hora = %s", sala, fecha, hora).fetchone():
        return "Esa sala ya está ocupada a esa hora."
    return None

app.jinja_env.filters["dmy"] = dmy

def cuentas():
    """CIF -> (nombre, rol). Se lee una vez por peticion."""
    # ponytail: trae todas las cuentas cada vez; filtrar por CIF si llegan a ser miles
    if "cuentas" not in g:
        g.cuentas = {f["cif"]: (f["nombre"], f["rol"]) for f in sql("SELECT cif, nombre, rol FROM cuentas")}
    return g.cuentas

def quien():
    """CIF, rol y nombre de quien entro; None si no hay sesion."""
    cif = session.get("cif")
    return (cif, *cuentas()[cif]) if cif in cuentas() else None

def ip_del_usuario():
    # En Fly la IP real llega en Fly-Client-IP; fuera de Fly ese encabezado lo podria inventar cualquiera
    return (request.headers.get("Fly-Client-IP") if "FLY_APP_NAME" in os.environ else None) or request.remote_addr or ""

def lugar_de(ip):
    """Ciudad, región y país aproximados de la IP (no la dirección exacta). Se busca una vez por sesión e IP."""
    guardado = session.get("lugar")
    if guardado and guardado[0] == ip:
        return guardado[1]
    try:
        if not ipaddress.ip_address(ip).is_global:
            lugar = "Red local"
        else:
            with urllib.request.urlopen(f"https://ipwho.is/{ip}?fields=success,city,region,country", timeout=3) as r:
                datos = json.load(r)
            partes = [datos[c] for c in ("city", "region", "country") if datos.get("success") and datos.get(c)]
            lugar = ", ".join(dict.fromkeys(partes))  # "Managua, Managua, Nicaragua" -> "Managua, Nicaragua"
    except (OSError, ValueError):  # sin respuesta del servicio o IP rara: la accion se anota igual
        lugar = ""
    session["lugar"] = [ip, lugar]
    return lugar

def anotar(accion):
    """Bitacora: deja constancia de lo que acaba de hacer el usuario, desde qué IP y lugar aproximado."""
    cif, nombre, rol = quien()
    ip = ip_del_usuario()
    sql("INSERT INTO bitacora (cif, nombre, rol, accion, ip, lugar) VALUES (%s, %s, %s, %s, %s, %s)",
        cif, nombre, ROLES[rol], accion, ip, lugar_de(ip))

def comun():
    """Datos que necesita la plantilla base en todas las paginas."""
    cif, nombre, rol = quien()
    return {"cif": cif, "nombre": nombre, "rol": ROLES[rol], "es_rol": rol, "pestanas": MENUS[rol]}

def paciente(pid):
    """El paciente, solo si el usuario puede verlo: cada estudiante ve unicamente los suyos."""
    cif, _, rol = quien()
    p = sql("SELECT * FROM pacientes WHERE id = %s", pid).fetchone()
    if p and rol == "estudiante" and p["estudiante"] != cif:
        return None
    return p

def mis_pacientes():
    # La firma solo importa para saber si ya firmo; no hace falta traer la imagen
    cif, _, rol = quien()
    return sql("SELECT id, nombre, cedula, telefono, razon, estudiante, aprobado, firma <> '' AS firma"
               " FROM pacientes WHERE %s OR estudiante = %s ORDER BY id", rol != "estudiante", cif).fetchall()

def nombre_de(cif):
    return cuentas()[cif][0] if cif in cuentas() else "sin asignar"

def todas_las_salas():
    return sql("SELECT * FROM salas ORDER BY id").fetchall()


@app.before_request
def exigir_sesion():
    if request.endpoint not in ("entrar", "static") and quien() is None:
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
            cuenta = sql("SELECT clave FROM cuentas WHERE cif = %s", cif).fetchone()
            if cuenta and check_password_hash(cuenta["clave"], clave):
                session.permanent = True  # cuenta los 30 min desde la ultima pagina que se abrio
                session["cif"] = cif
                anotar("Entró al sistema")
                return redirect(url_for("menu"))
            datos["aviso"] = "CIF o contraseña incorrectos."
    return render_template("entrar.html", **datos)


@app.route("/menu")
def menu():
    return render_template("menu.html", **comun(), que_hace=QUE_HACE[quien()[2]],
                           en_proceso=list(EN_PROCESO.items()))


@app.route("/pacientes", methods=["GET", "POST"])
def pacientes():
    cif, _, rol = quien()
    form = {}
    if request.method == "POST":
        accion = request.form.get("accion")
        if accion == "registrar" and rol == "estudiante":
            form = {c: request.form.get(c, "").strip() for c in ("nombre", "cedula", "telefono")}
            if not form["nombre"] or not form["cedula"]:
                flash("El nombre y la cédula del paciente son obligatorios.", "error")
            elif sql("SELECT 1 FROM pacientes WHERE cedula = %s", form["cedula"]).fetchone():
                flash("Ya existe un paciente con esa cédula.", "error")
            else:
                with bd().transaction():
                    pid = sql("INSERT INTO pacientes (nombre, cedula, telefono, estudiante) VALUES (%s, %s, %s, %s)"
                              " RETURNING id", form["nombre"], form["cedula"], form["telefono"], cif).fetchone()["id"]
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
        form = {c: request.form.get(c, "").strip() for c in ("nombre", "cedula", "telefono")}
        p.update(form)  # si hay error, el formulario vuelve con lo que se escribio
        if not form["nombre"] or not form["cedula"]:
            flash("El nombre y la cédula del paciente son obligatorios.", "error")
        elif sql("SELECT 1 FROM pacientes WHERE cedula = %s AND id <> %s", form["cedula"], pid).fetchone():
            flash("Ya existe otro paciente con esa cédula.", "error")
        else:
            sql("UPDATE pacientes SET nombre = %s, cedula = %s, telefono = %s WHERE id = %s",
                form["nombre"], form["cedula"], form["telefono"], pid)
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
    return render_template("expediente.html", **comun(), p=p, nombre_de=nombre_de,
                           citas=sql("SELECT * FROM citas WHERE paciente_id = %s", pid).fetchall())


@app.route("/pacientes/<int:pid>/traspaso", methods=["GET", "POST"])
def traspaso(pid):
    """Cambiar de estudiante: el trabajo del anterior se archiva con el resumen de lo que le hizo."""
    p = paciente(pid)
    if not p or quien()[2] != "admin":
        return redirect(url_for("pacientes"))
    estudiantes = [(c, n) for c, (n, r) in cuentas().items() if r == "estudiante"]
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


@app.route("/pacientes/<int:pid>/consentimiento", methods=["GET", "POST"])
def consentimiento(pid):
    """Consentimiento del paciente, que firma con el dedo o el mouse. El docente no firma."""
    p = paciente(pid)
    if not p:
        return redirect(url_for("pacientes"))
    if request.method == "POST" and quien()[2] == "estudiante":
        firma = request.form.get("firma", "")
        if not firma.startswith("data:image/png;base64,"):
            flash("Falta la firma del paciente: dibújela con el dedo o el mouse.", "error")
        else:
            sql("UPDATE pacientes SET firma = %s, firma_fecha = %s WHERE id = %s", firma, hoy(), pid)
            anotar(f"Recogió la firma del consentimiento de {p['nombre']}")
            flash("Consentimiento firmado. Ya se puede imprimir.", "ok")
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
    """Plano de la clínica: cada sala con un check si está disponible o una X si no."""
    if request.method == "POST" and quien()[2] == "admin":
        if request.form.get("accion") == "agregar":
            nombre = request.form.get("nombre", "").strip()
            if not nombre:
                flash("Escriba el nombre de la sala.", "error")
            elif sql("SELECT 1 FROM salas WHERE lower(nombre) = lower(%s)", nombre).fetchone():
                flash("Ya existe una sala con ese nombre.", "error")
            else:
                sql("INSERT INTO salas (nombre) VALUES (%s)", nombre)
                anotar(f"Agregó {nombre}")
                flash(f"{nombre} agregada.", "ok")
        else:
            s = sql("UPDATE salas SET disponible = NOT disponible WHERE id = %s RETURNING nombre, disponible",
                    request.form.get("sala", type=int)).fetchone()
            if s:
                anotar(f"Marcó {s['nombre']} como " + ("disponible" if s["disponible"] else "no disponible"))
        return redirect(url_for("salas"))
    return render_template("salas.html", **comun(), salas=todas_las_salas())


@app.route("/bitacora")
def bitacora():
    """Quién hizo qué y a qué hora. La revisan el profesor y la Facultad."""
    if quien()[2] == "estudiante":
        return redirect(url_for("menu"))
    # ponytail: solo las ultimas 500 lineas; paginar si hace falta revisar mas atras
    lineas = sql("SELECT to_char(cuando AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS cuando,"
                 " cif, nombre, rol, accion, ip, lugar FROM bitacora ORDER BY id DESC LIMIT 500").fetchall()
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
    for etiqueta, valor in (("Nombre", p["nombre"]), ("Cédula", p["cedula"]), ("Teléfono", p["telefono"] or "—"),
                            ("Razón de la consulta", p["razon"] or "—"),
                            ("Estudiante a cargo", nombre_de(p["estudiante"])),
                            ("Estado", "Aprobado" if p["aprobado"] else "Pendiente de aprobación")):
        linea(f"{etiqueta}: {valor}")

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
            linea(f"- {dmy(c['fecha'])} {c['hora']} · {c['razon']} · {c['sala'] or 'sin sala'}", "Helvetica", 10)

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
