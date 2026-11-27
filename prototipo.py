"""Clínicas Odontológicas (Flask + HTML), con los datos en Neon (PostgreSQL en la nube).

Necesita:  pip install -r requirements.txt
Variables: DATABASE_URL (cadena de conexión de Neon), SECRET_KEY, el almacenamiento de las fotos (ALMACEN_*, ver
           abajo) y, opcionales, CLAVE_INICIAL, SITIO y COMENTARIOS
Ejecutar:  python prototipo.py   (crea las tablas si no existen)
"""
import atexit
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import boto3
import click
import psycopg
from botocore.config import Config
from flask import (Flask, Response, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from PIL import Image, ImageOps
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from reportlab.graphics import renderSVG
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from werkzeug.security import check_password_hash as _revisar, generate_password_hash as _cifrar

# Cuentas iniciales: dos por cada tipo y una de la Facultad. Se crean una sola vez en la base de datos.
# El modelo pide correo y, al estudiante, su año de la carrera; la Facultad los corrige en la ficha de la cuenta
CUENTAS_INICIALES = {"00000001": ("Dra. Grace Mendoza", "profesor", None),
                     "00000004": ("Dr. Gustavo Rivas", "profesor", None),
                     "00000002": ("Grace Alvarado", "estudiante", 1),
                     "00000005": ("Gabriel Espinoza", "estudiante", 1),
                     "00000007": ("Gina Saavedra", "facultad", None)}
CLAVE_INICIAL = os.environ.get("CLAVE_INICIAL", "uam2026")  # se guarda cifrada, nunca en texto plano
CORREO_PENDIENTE = "@correo-pendiente.uam"  # correo provisional de una cuenta que todavía no tiene el suyo
# Dirección pública del sistema: es la que va en el QR de las clases, para que abra desde el celular del
# estudiante aunque el profesor tenga el sistema abierto en otra dirección (por ejemplo, localhost)
SITIO = os.environ.get("SITIO", "https://clinicas-odontologicas-uam.fly.dev").rstrip("/")
# Enlace del formulario de Google donde estudiantes y profesores dejan sus observaciones para mejorar el
# sistema. Si se deja vacío (o no es una dirección https), la pestaña Comentarios no aparece
COMENTARIOS = os.environ.get("COMENTARIOS", "https://docs.google.com/forms/d/e/1FAIpQLSe1BhT__8TPsUv_Iowl4nalISPvR-"
                                            "0GqE3LUA0LYqvooZVDjw/viewform").strip()
COMENTARIOS = COMENTARIOS if COMENTARIOS.startswith("https://") else ""
ROLES = {"profesor": "Profesor", "estudiante": "Estudiante", "facultad": "Facultad"}
# Pestanas de cada rol: (nombre, endpoint). La bitacora la revisan el profesor y la Facultad
MENUS = {"estudiante": [("Inicio", "menu"), ("Mis pacientes", "pacientes"), ("Mis citas", "citas"), ("Clínicas", "salas"),
                        ("Materiales", "materiales"), ("Hoja de control", "control"), ("Comentarios", "comentarios")],
         # El profesor supervisa: sus clases, las citas y tratamientos de sus estudiantes y los recibos
         "profesor": [("Inicio", "menu"), ("Mis clases", "clases"), ("Pacientes", "pacientes"), ("Citas", "citas"),
                      ("Clínicas", "salas"), ("Hoja de control", "control"), ("Bitácora", "bitacora"),
                      ("Comentarios", "comentarios")],
         "facultad": [("Inicio", "menu"), ("Pacientes", "pacientes"), ("Citas", "citas"), ("Clínicas", "salas"),
                      ("Tarifario", "tarifario"), ("Inventario", "inventario"), ("Cuentas", "administrar_cuentas"),
                      ("Clases", "clases"), ("Hoja de control", "control"), ("Bitácora", "bitacora")]}
QUE_HACE = {"estudiante": ["Entrar a la clase de su profesor con el código QR que él le da",
                           "Registrar a sus pacientes y la razón por la que vienen",
                           "Agendar sus citas (las aprueba su profesor), confirmarlas, reprogramarlas o cancelarlas",
                           "Marcar si el paciente llegó a su cita o no se presentó",
                           "Subir las fotos del paciente, llenar el odontograma y pedir la revisión del profesor",
                           "Llenar la historia clínica y anotar el plan de tratamiento por fases",
                           "Pedir materiales a proveeduría",
                           "Recoger la firma del paciente en el consentimiento informado e imprimirlo",
                           "Subir la foto de cada recibo de pago y entregar impresa la hoja de control"],
            "profesor": ["Crear sus clases y los códigos QR con los que entran sus estudiantes",
                         "Aprobar a los pacientes que registra cada estudiante o rechazarlos con una observación",
                         "Aprobar las citas que agendan sus estudiantes, o agendarlas él mismo (ya aprobadas)",
                         "Aprobar los tratamientos que realizan sus estudiantes",
                         "Aprobar el odontograma o devolverlo con lo que hay que corregir",
                         "Generar la orden de cobro de un paciente y firmar los recibos de pago",
                         "Revisar la bitácora del sistema"],
            "facultad": ["Crear, corregir y desactivar las cuentas, y poner a un estudiante en una clase",
                         "Entregar los materiales que piden los estudiantes y llevar las entradas y salidas del inventario",
                         "Administrar las clínicas, el tarifario y resolver las fallas reportadas",
                         "Generar las órdenes de cobro y registrar los pagos de los pacientes",
                         "Cambiar un paciente de estudiante con el resumen del trabajo del anterior",
                         "Ver todo lo que pasa en el sistema y revisar la bitácora"]}

# Odontograma: numeracion FDI de los 32 dientes permanentes, como se ven de frente al paciente
ARRIBA = [*range(18, 10, -1), *range(21, 29)]
ABAJO = [*range(48, 40, -1), *range(31, 39)]
DIENTES = set(ARRIBA + ABAJO)
# Estado de cada diente (dientes.estado): valor guardado -> (como se muestra, color)
ESTADOS = {"sano": ("Sano", "#ffffff"), "caries": ("Caries", "#e57373"), "obturado": ("Obturado", "#64b5f6"),
           "endodoncia": ("Endodoncia", "#ba68c8"), "corona": ("Corona", "#ffd54f"),
           "extraccion": ("Extracción indicada", "#ffb74d"), "ausente": ("Ausente", "#ffffff")}
SUPERFICIES = {"oclusal": "Oclusal", "mesial": "Mesial", "distal": "Distal", "vestibular": "Vestibular",
               "lingual": "Lingual"}
# Ciclo de vida de una cita (RF11): de cada estado activo, a cuales puede pasar. Las demas ya no cambian
ESTADOS_CITA = {"programada": "Programada", "confirmada": "Confirmada", "atendida": "Atendida",
                "cancelada": "Cancelada", "no_asistio": "No se presentó"}
ACTIVAS = ("programada", "confirmada")
SIGUIENTE = {"programada": ("confirmada", "atendida", "no_asistio", "cancelada"),
             "confirmada": ("atendida", "no_asistio", "cancelada")}
COLOR_CITA = {"programada": "", "confirmada": "verde", "atendida": "verde", "cancelada": "tenue", "no_asistio": "rojo"}
# Horario de la clínica (lo exige la base): lunes a sábado de 07:00 a 16:00; una cita dura hasta 4 horas
APERTURA, CIERRE = "07:00", "16:00"
DURACIONES = (30, 45, 60, 90, 120, 180, 240)  # minutos que se pueden elegir para una cita
FASES = {"urgencia": "Urgencia", "basica": "Básica", "rehabilitacion": "Rehabilitación", "mantenimiento": "Mantenimiento"}
TIPOS_FALLA = {"equipo": "Equipo", "electricidad": "Electricidad", "agua": "Agua", "mobiliario": "Mobiliario",
               "limpieza": "Limpieza", "otro": "Otro"}
AREAS = ("Diagnóstico", "Periodoncia", "Operatoria", "Endodoncia", "Cirugía", "Prótesis", "Odontopediatría", "Otra")
SEXOS = {"F": "Femenino", "M": "Masculino"}
TIPOS_FOTO = {"intraoral": "Intraoral", "extraoral": "Extraoral", "radiografia": "Radiografía"}
METODOS_PAGO = {"efectivo": "Efectivo", "tarjeta": "Tarjeta", "transferencia": "Transferencia"}
# Campos de la historia clinica (RF05) que van en la tabla historias: columna -> como se muestra.
# La fecha de nacimiento y el sexo son datos del paciente (tabla pacientes) y se llenan en el mismo formulario
HISTORIA = {"direccion": "Dirección", "emergencia_nombre": "Contacto de emergencia",
            "emergencia_telefono": "Teléfono de emergencia", "antecedentes": "Antecedentes médicos",
            "alergias": "Alergias", "medicamentos": "Medicamentos que toma",
            "antecedentes_odontologicos": "Antecedentes odontológicos", "presion": "Presión arterial",
            "examen_extraoral": "Examen extraoral", "examen_intraoral": "Examen intraoral",
            "diagnostico": "Diagnóstico"}
DEL_PACIENTE = {"nacimiento": "Fecha de nacimiento", "sexo": "Sexo"}
# Para marcarla completa (RF05) estos no pueden quedar vacios: si no hay alergias se escribe "Ninguna"
HISTORIA_COMPLETA = ("nacimiento", "sexo", "emergencia_nombre", "emergencia_telefono", "antecedentes", "alergias",
                     "medicamentos", "presion", "examen_intraoral", "diagnostico")
IMAGEN_MAXIMA = 5 * 1024 * 1024  # RNF09: fotos JPG o PNG de hasta 5 MB
REVISIONES = {"": "Sin pedir", "pedida": "Pedida, esperando al profesor", "aprobada": "Aprobada",
              "devuelta": "Devuelta para corregir"}

# El esquema de la base (modelo de datos v14 y sus extensiones) esta en base-de-datos/esquema.sql
ESQUEMA = (Path(__file__).parent / "base-de-datos" / "esquema.sql").read_text(encoding="utf-8")

app = Flask(__name__)
# Sin SECRET_KEY las sesiones se pierden al reiniciar, pero nunca quedan firmadas con una clave conocida
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE="FLY_APP_NAME" in os.environ,
                  # Las computadoras de la clinica son compartidas: la sesion se cierra tras 15 min sin uso (RNF05)
                  PERMANENT_SESSION_LIFETIME=timedelta(minutes=15),
                  # la foto presencial de un celular pesa unos MB; se achica antes de guardarla
                  MAX_CONTENT_LENGTH=12_000_000)

FORMATO = "%d/%m/%Y"  # las fechas se muestran como dd/mm/aaaa; se eligen en el calendario
MANAGUA = timezone(timedelta(hours=-6))  # Nicaragua no cambia de horario; el servidor esta en UTC
GRUPOS = {1: "Clínicas pregrado", 2: "Clínicas posgrado"}  # valor de salas.en_sala
# Las salas reales, según el plano del edificio: pregrado tiene 30 unidades (3 filas de 10) y posgrado 20
# (columnas de 7, 7 y 6). Se numeran seguidas: de la 1 a la 30 pregrado y de la 31 a la 50 posgrado
PREGRADO, POSGRADO = 30, 20
MOTIVO_MAXIMO = 500  # caracteres del motivo de una clinica que no funciona
RECIBO_MAXIMO = 40  # caracteres del numero de recibo
FALLOS_MAXIMOS, MINUTOS_BLOQUEO = 5, 15  # tras 5 claves equivocadas seguidas la cuenta espera 15 min
CLAVE_MINIMA = 8  # caracteres de la contraseña que la Facultad le pone a una cuenta
CODIGOS_MAXIMO, DIAS_MAXIMO = 200, 30  # de una clase: cuántos códigos QR se generan a la vez y cuánto duran
CIF_MAXIMO, NOMBRE_MAXIMO = 20, 80  # caracteres de lo que escribe el estudiante al registrarse
FOTO_LADO = 1600  # pixeles del lado mas largo de la foto presencial guardada
DIAS = {1: "Lunes", 2: "Martes", 3: "Miércoles", 4: "Jueves", 5: "Viernes", 6: "Sábado"}  # turnos.dia (ISO)
PERIODO = re.compile(r"\d{4}-(I|II|III)")  # periodo de una clase, por ejemplo 2026-II
ANIOS = range(1, 7)  # año de la carrera
CORREO = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


_conexiones = []
_candado_conexiones = threading.Lock()

def bd():
    """Una conexion a Neon por peticion. Se toman de un grupo que las reutiliza: abrir una nueva en cada pagina
    (con su cifrado TLS) era lo mas lento con muchos usuarios a la vez (RNF02)."""
    if "bd" not in g:
        with _candado_conexiones:  # el grupo se crea en cada proceso de gunicorn, al primer uso
            if not _conexiones:
                _conexiones.append(ConnectionPool(
                    os.environ["DATABASE_URL"], min_size=1, max_size=4, check=ConnectionPool.check_connection,
                    kwargs={"autocommit": True, "row_factory": dict_row,
                            "prepare_threshold": None}))  # el pooler de Neon no admite sentencias preparadas
                atexit.register(_conexiones[0].close)  # al terminar un comando (crear-tablas) se cierra en orden
        g.bd = _conexiones[0].getconn()
    return g.bd

@app.teardown_appcontext
def cerrar_bd(_):
    if (conexion := g.pop("bd", None)) is not None:
        _conexiones[0].putconn(conexion)

# Cada clave cifrada con scrypt usa 32 MB de memoria mientras se calcula. Con muchos entrando a la vez (al empezar
# un turno) la maquina de 512 MB se quedaba sin memoria; de a dos por proceso alcanza y no se cae (RNF02)
_claves = threading.BoundedSemaphore(2)

def cifrar(clave):
    with _claves:
        return _cifrar(clave)

def clave_correcta(cifrada, clave):
    with _claves:
        return _revisar(cifrada, clave)

def sql(consulta, *args):
    # Sin argumentos no se buscan marcadores: asi un % escrito en la consulta (o en esquema.sql) es un % normal
    return bd().execute(consulta, args or None)


def crear_tablas():
    with app.app_context():
        if "FLY_APP_NAME" in os.environ and not ALMACEN["bucket"]:
            # En Fly el disco se borra en cada reinicio: sin un almacenamiento de verdad las fotos se perderian.
            # Fallar aqui detiene el despliegue y la version anterior sigue funcionando
            raise SystemExit("Falta configurar el almacenamiento de fotos: ALMACEN_BUCKET, ALMACEN_ENDPOINT,"
                             " ALMACEN_ID y ALMACEN_CLAVE (fly secrets set ...).")
        if modelo_anterior():
            # Fallar aqui detiene el despliegue: la base se pasa al modelo nuevo a proposito, nunca sola
            raise SystemExit("La base tiene el modelo de datos anterior. Páselo al modelo v14 en una rama nueva de"
                             " Neon con `flask --app prototipo migrar-v14` y use esa rama.")
        if not sql("SELECT to_regclass('clases') AS t").fetchone()["t"]:
            with bd().transaction():  # el esquema entero o nada
                sql(ESQUEMA)
        for cif, (nombre, rol, anio) in CUENTAS_INICIALES.items():
            sql("INSERT INTO cuentas (cif, nombre, correo, anio, rol, clave_hash) VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (cif) DO NOTHING", cif, nombre, cif + CORREO_PENDIENTE, anio, rol, cifrar(CLAVE_INICIAL))
        # Las clínicas se crean una sola vez: 30 de pregrado y 20 de posgrado, como en el plano del edificio
        if not sql("SELECT 1 FROM salas LIMIT 1").fetchone():
            with bd().transaction():
                for n in range(1, PREGRADO + POSGRADO + 1):
                    sql("INSERT INTO salas (nombre, en_sala) VALUES (%s, %s)", f"Clínica {n}", 1 if n <= PREGRADO else 2)

def modelo_anterior():
    """True si la base es del modelo anterior al v14 (cuentas sin columna id)."""
    return bool(sql("SELECT 1 FROM information_schema.tables t WHERE t.table_schema = current_schema()"
                    " AND t.table_name = 'cuentas' AND NOT EXISTS (SELECT 1 FROM information_schema.columns c"
                    " WHERE c.table_schema = t.table_schema AND c.table_name = 'cuentas' AND c.column_name = 'id')"
                    ).fetchone())

@app.cli.command("crear-tablas")
def crear_tablas_cli():
    crear_tablas()


# RNF07: respaldo diario propio. Neon solo deja volver 6 horas atras en el plan gratis y no programa copias,
# asi que la maquina respaldo-diario de Fly corre `flask --app prototipo respaldar` cada dia (docs/operacion/respaldos.md).
# El respaldo es un .zip con cada tabla en formato COPY y va al mismo almacen privado de las fotos.
RESPALDOS_GUARDADOS = 30  # ponytail: arma el .zip en memoria; pasar a archivo temporal si la base pasa de ~100 MB

def tablas_en_orden():
    """Las tablas de la base, cada una despues de las que referencia, para poder cargarlas en ese orden.
    Las llaves diferibles (cuentas.clase_id) se revisan al final de la carga y no cuentan para el orden."""
    from graphlib import TopologicalSorter
    orden = TopologicalSorter({t["tablename"]: set() for t in sql(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")})
    for r in sql("SELECT conrelid::regclass::text AS hija, confrelid::regclass::text AS madre FROM pg_constraint"
                 " WHERE contype = 'f' AND connamespace = current_schema()::regnamespace AND conrelid <> confrelid"
                 " AND NOT condeferrable"):
        orden.add(r["hija"], r["madre"])
    return list(orden.static_order())

def columnas_de(tabla):
    return [c["column_name"] for c in sql("SELECT column_name FROM information_schema.columns WHERE table_schema ="
                                          " current_schema() AND table_name = %s ORDER BY ordinal_position", tabla)]

@app.cli.command("respaldar")
def respaldar():
    import zipfile
    archivo = io.BytesIO()
    orden = tablas_en_orden()
    with zipfile.ZipFile(archivo, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("orden.json", json.dumps({t: columnas_de(t) for t in orden}))
        with bd().transaction(), bd().cursor() as cur:  # una sola foto de la base, aunque alguien escriba
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            for t in orden:
                with cur.copy(f'COPY "{t}" TO STDOUT') as copia:
                    z.writestr(f"{t}.copy", b"".join(bytes(b) for b in copia))
    nombre = f"respaldos/{datetime.now(MANAGUA):%Y-%m-%d_%H%M}.zip"
    if ALMACEN["bucket"]:
        s3().put_object(Bucket=ALMACEN["bucket"], Key=nombre, Body=archivo.getvalue())
        viejos = sorted(o["Key"] for o in s3().list_objects_v2(Bucket=ALMACEN["bucket"], Prefix="respaldos/")
                        .get("Contents", []))[:-RESPALDOS_GUARDADOS]
        for clave in viejos:
            s3().delete_object(Bucket=ALMACEN["bucket"], Key=clave)
    else:
        (CARPETA_LOCAL / nombre).parent.mkdir(parents=True, exist_ok=True)
        (CARPETA_LOCAL / nombre).write_bytes(archivo.getvalue())
    print(f"Respaldo guardado: {nombre} ({len(archivo.getvalue()) // 1024} KB, {len(orden)} tablas)")

@app.cli.command("restaurar")
@click.argument("nombre")
def restaurar(nombre):
    """Carga un respaldo en la base de DATABASE_URL, que tiene que estar vacia (una rama nueva de Neon)."""
    import zipfile
    if sql("SELECT to_regclass('cuentas') AS t").fetchone()["t"] and sql("SELECT 1 FROM cuentas LIMIT 1").fetchone():
        raise SystemExit("Esa base ya tiene datos. Restaure en una rama nueva y vacía de Neon, nunca encima de otra.")
    if ALMACEN["bucket"]:
        datos = s3().get_object(Bucket=ALMACEN["bucket"], Key=nombre)["Body"].read()
    else:
        datos = (CARPETA_LOCAL / nombre).read_bytes()
    if not sql("SELECT to_regclass('clases') AS t").fetchone()["t"]:
        sql(ESQUEMA)  # las tablas de esta version del sistema, vacias
    with zipfile.ZipFile(io.BytesIO(datos)) as z, bd().transaction(), bd().cursor() as cur:
        # Los datos ya pasaron las reglas cuando se guardaron: las reglas automaticas no se repiten al cargarlos
        cur.execute("SET LOCAL clinicas.restaurando = 'si'")
        orden = json.loads(z.read("orden.json"))
        cur.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in orden) + " RESTART IDENTITY CASCADE")
        for t, cols in orden.items():
            with cur.copy(f'COPY "{t}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) FROM STDIN') as copia:
                copia.write(z.read(f"{t}.copy"))
        # Los numeros automaticos siguen despues del ultimo que habia
        for s in cur.execute("SELECT table_name AS t, column_name AS c FROM information_schema.columns WHERE"
                             " table_schema = current_schema() AND column_default LIKE 'nextval%'").fetchall():
            cur.execute(f'SELECT setval(pg_get_serial_sequence(%s, %s), coalesce((SELECT max("{s["c"]}") FROM'
                        f' "{s["t"]}"), 0) + 1, false)', (s["t"], s["c"]))
    cuantas = {t: sql(f'SELECT count(*) AS n FROM "{t}"').fetchone()["n"] for t in orden}
    print(f"Restaurado {nombre}: " + ", ".join(f"{t} {n}" for t, n in cuantas.items()))


def hoy():
    return datetime.now(MANAGUA).date().isoformat()

def dmy(iso):
    """2026-10-28 -> 28/10/2026, el formato que se usa aqui."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime(FORMATO)
    except (ValueError, TypeError):
        return iso or ""

def cedula_nica(texto):
    """Cédula nicaragüense 000-000000-0000A (RNF08), con o sin guiones; None si no lo es. Los 6 del medio
    son la fecha de nacimiento (ddmmaa), así que tienen que ser una fecha."""
    t = re.sub(r"[\s-]", "", (texto or "").upper())
    if not re.fullmatch(r"\d{13}[A-Z]", t):
        return None
    try:
        datetime.strptime(t[3:9], "%d%m%y")
    except ValueError:
        return None
    return f"{t[:3]}-{t[3:9]}-{t[9:]}"

def telefono_nica(texto):
    """Teléfono de 8 dígitos (RNF08), con o sin espacios, guiones o +505. '' si se deja en blanco; None si no sirve."""
    t = re.sub(r"[\s-]", "", texto or "")
    if t.startswith("+505"):
        t = t[4:]
    return t if re.fullmatch(r"\d{8}", t) or t == "" else None

def sin_signos(cedula):
    """La cédula solo con números y letras, para comparar una escrita con guiones con otra sin ellos."""
    return re.sub(r"[^0-9A-Z]", "", (cedula or "").upper())

def error_de_paciente(form, pid=None):
    """Por qué no se pueden guardar los datos del paciente; None si se pueden. Deja la cédula y el teléfono
    con el formato de siempre."""
    if not form["nombre"] or not form["cedula"]:
        return "El nombre y la cédula del paciente son obligatorios."
    cedula, telefono = cedula_nica(form["cedula"]), telefono_nica(form["telefono"])
    if not cedula:
        return "La cédula debe tener el formato 000-000000-0000A (los 6 números del medio son la fecha de nacimiento)."
    if telefono is None:
        return "El teléfono debe tener 8 dígitos."
    form["cedula"], form["telefono"] = cedula, telefono
    otro = sql("SELECT eliminado_en FROM pacientes WHERE regexp_replace(upper(cedula), '[^0-9A-Z]', '', 'g') = %s"
               " AND id <> %s", sin_signos(cedula), pid or 0).fetchone()
    if form.get("expediente") and sql("SELECT 1 FROM pacientes WHERE expediente = %s AND id <> %s",
                                      form["expediente"], pid or 0).fetchone():
        return "Ese número de expediente ya es de otro paciente."
    if otro and otro["eliminado_en"]:
        return "Esa cédula es de un paciente eliminado. Pida a la Facultad que lo restaure."
    if otro:
        return "Ya existe otro paciente con esa cédula." if pid else "Ya existe un paciente con esa cédula."
    return None

def rango_cita(fecha, hora, minutos):
    """(inicio, fin) de una cita con fecha aaaa-mm-dd, hora HH:MM y duracion en minutos; None si no se entiende."""
    try:
        inicio = datetime.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M").replace(tzinfo=MANAGUA)
    except ValueError:
        return None
    return (inicio, inicio + timedelta(minutes=minutos)) if minutos in DURACIONES else None

def error_de_cita(rango, sala_id, paciente_id, estudiante_id, cita_id=None):
    """Por qué no se puede agendar la cita en ese rango (inicio, fin); None si se puede. Lo mismo que exige la
    base (horario de la clínica, clínica y paciente sin dos citas a la vez), con un mensaje claro, y además que el
    estudiante no tenga otra cita encimada y que caiga en sus turnos (RF10). Las citas canceladas o a las que el
    paciente no llegó no cuentan, y al reprogramar tampoco la cita misma (cita_id)."""
    if not rango:
        return "Elija la fecha en el calendario, la hora y cuánto dura la cita."
    inicio, fin = rango
    if inicio < datetime.now(MANAGUA):
        return "No se pueden agendar citas en una fecha u hora que ya pasó."
    if inicio.isoweekday() == 7 or inicio.strftime("%H:%M") < APERTURA or fin.strftime("%H:%M") > CIERRE             or fin.date() != inicio.date():
        return f"La clínica atiende de lunes a sábado, de {APERTURA} a {CIERRE}: la cita tiene que caer en ese horario."
    sala = sql("SELECT nombre, habilitada FROM salas WHERE id = %s", sala_id).fetchone()
    if not sala:
        return "Elija la clínica."
    if not sala["habilitada"]:
        return "Esa clínica no está habilitada."
    otra = ("SELECT 1 FROM citas c WHERE c.estado IN ('programada', 'confirmada', 'atendida') AND c.id <> %s"
            " AND tstzrange(c.inicio, c.fin) && tstzrange(%s, %s) AND ")
    if sql(otra + "c.sala_id = %s", cita_id or 0, inicio, fin, sala_id).fetchone():
        return f"La {sala['nombre']} ya está ocupada a esa hora."
    if sql(otra + "c.paciente_id = %s", cita_id or 0, inicio, fin, paciente_id).fetchone():
        return "El paciente ya tiene otra cita a esa hora."
    if sql(otra + "c.estudiante_id = %s", cita_id or 0, inicio, fin, estudiante_id).fetchone():
        return "El estudiante ya tiene otra cita a esa hora con otro paciente."
    turnos = turnos_de(estudiante_id)
    if turnos and not any(t["dia"] == inicio.isoweekday() and t["desde"] <= inicio.time() and fin.time() <= t["hasta"]
                          for t in turnos):
        return "Esa hora está fuera de los turnos clínicos del estudiante: " + ", ".join(
            f"{DIAS[t['dia']]} de {t['desde']:%H:%M} a {t['hasta']:%H:%M}" for t in turnos) + "."
    return None

def turnos_de(estudiante_id):
    """RF10: los turnos clinicos del estudiante, por dia y hora."""
    return sql("SELECT * FROM turnos WHERE estudiante_id = %s ORDER BY dia, desde", estudiante_id).fetchall()

# Las citas con su fecha, hora y clinica a la vista (la base guarda inicio y fin con su zona horaria)
CITAS = ("SELECT c.*, to_char(c.inicio AT TIME ZONE 'America/Managua', 'YYYY-MM-DD') AS fecha,"
         " to_char(c.inicio AT TIME ZONE 'America/Managua', 'HH24:MI') AS hora,"
         " to_char(c.fin AT TIME ZONE 'America/Managua', 'HH24:MI') AS hora_fin,"
         " (extract(epoch FROM c.fin - c.inicio) / 60)::int AS minutos, s.nombre AS sala"
         " FROM citas c JOIN salas s ON s.id = c.sala_id")

app.jinja_env.filters["dmy"] = dmy
app.jinja_env.globals["GRUPOS"] = GRUPOS
app.jinja_env.globals["MANAGUA"] = MANAGUA

def whatsapp(nombre, telefono, cita):
    """RF12: enlace de WhatsApp con el recordatorio escrito, para que el estudiante lo envie con un toque.
    El envio automatico necesitaria una cuenta de WhatsApp Business API; esto funciona sin ella."""
    if not telefono:
        return ""
    texto = (f"Hola, {nombre}. Le recordamos su cita en las Clínicas Odontológicas de la UAM el {dmy(cita['fecha'])}"
             f" a las {cita['hora']}" + (f" en la {cita['sala']}" if cita["sala"] else "") + ". ¿Nos confirma que viene?")
    return f"https://wa.me/505{telefono}?text={urllib.parse.quote(texto)}"

app.jinja_env.globals["whatsapp"] = whatsapp

# RF12: recordatorio automatico con la API oficial de WhatsApp (Cloud API de Meta). Lo corre cada hora la
# maquina recordatorios de Fly: `flask --app prototipo recordar` (ver docs/operacion/recordatorios-whatsapp.md)
HORARIO_RECORDATORIOS = (8, 18)  # solo se envia de 8:00 a 18:00 de Managua

def enviar_whatsapp(telefono, variables, cita_id):
    """Envia la plantilla aprobada en Meta, con sus dos botones de respuesta (asistira / no podra). Cada boton
    lleva el numero de la cita, para saber a cual responde el paciente. Lanza una excepcion si Meta lo rechaza."""
    botones = [{"type": "button", "sub_type": "quick_reply", "index": str(i),
                "parameters": [{"type": "payload", "payload": f"{accion}:{cita_id}"}]}
               for i, accion in enumerate(("asistira", "no_asistira"))]
    return _a_whatsapp({"messaging_product": "whatsapp", "to": f"505{telefono}", "type": "template",
                        "template": {"name": os.environ["WHATSAPP_PLANTILLA"], "language": {"code": "es"},
                                     "components": [{"type": "body", "parameters": [{"type": "text", "text": v}
                                                                                    for v in variables]}]
                                     + botones}})

# RF12: aviso inmediato al paciente cuando el estudiante le agenda o reprograma una cita. Usa la plantilla
# cita_agendada aprobada en Meta: "Hola, {{1}}. Su cita en las Clinicas Odontologicas de la UAM quedo agendada
# para el {{2}} a las {{3}} en {{4}}. Si necesita cambiarla, responda a este mensaje."
PLANTILLA_AGENDADA = os.environ.get("WHATSAPP_PLANTILLA_AGENDADA", "cita_agendada")

def enviar_aviso_cita(telefono, variables):
    return _a_whatsapp({"messaging_product": "whatsapp", "to": f"505{telefono}", "type": "template",
                        "template": {"name": PLANTILLA_AGENDADA, "language": {"code": "es"},
                                     "components": [{"type": "body", "parameters": [{"type": "text", "text": v}
                                                                                    for v in variables]}]}})

def avisar_cita(p, fecha, hora, sala, que):
    """Manda el aviso si el paciente tiene telefono y WhatsApp esta configurado. Si Meta falla, la cita ya quedo
    guardada: solo se anota que el aviso no salio."""
    if not (p["telefono"] and os.environ.get("WHATSAPP_TOKEN") and os.environ.get("WHATSAPP_NUMERO_ID")):
        return
    cuando = f"{dmy(fecha)} a las {hora}"
    try:
        enviar_aviso_cita(p["telefono"], [p["nombre"].split()[0], dmy(fecha), hora, sala or "la clínica"])
        hecho = f"Aviso por WhatsApp a {p['nombre']}: cita {que} para el {cuando}"
    except Exception as e:  # noqa: BLE001 - la cita no depende del aviso
        app.logger.warning("No se pudo avisar la cita por WhatsApp: %s", e)
        hecho = f"No se pudo avisar por WhatsApp a {p['nombre']} de su cita del {cuando}"
    anotar_sistema(hecho)

def anotar_sistema(accion):
    """Bitacora de lo que hace el sistema solo (recordatorios y respuestas por WhatsApp): sin cuenta."""
    sql("INSERT INTO bitacora (cif_registrado, accion) VALUES ('SISTEMA', %s)", accion)

def responder_whatsapp(telefono, texto):
    """Respuesta de texto al paciente. Va dentro de las 24 h en que el paciente escribio, sin plantilla."""
    return _a_whatsapp({"messaging_product": "whatsapp", "to": telefono, "type": "text", "text": {"body": texto}})

def _a_whatsapp(cuerpo):
    pedido = urllib.request.Request(
        f"https://graph.facebook.com/v25.0/{os.environ['WHATSAPP_NUMERO_ID']}/messages",
        data=json.dumps(cuerpo).encode(), method="POST",
        headers={"Authorization": f"Bearer {os.environ['WHATSAPP_TOKEN']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(pedido, timeout=15) as r:
        return json.load(r)["messages"][0]["id"]

@app.cli.command("recordar")
def recordar():
    if not all(os.environ.get(v) for v in ("WHATSAPP_TOKEN", "WHATSAPP_NUMERO_ID", "WHATSAPP_PLANTILLA")):
        print("WhatsApp no está configurado: faltan WHATSAPP_TOKEN, WHATSAPP_NUMERO_ID y WHATSAPP_PLANTILLA.")
        return
    ahora = datetime.now(MANAGUA)
    if not HORARIO_RECORDATORIOS[0] <= ahora.hour < HORARIO_RECORDATORIOS[1]:
        print(f"Fuera del horario de envío ({ahora:%H:%M}).")
        return
    manana = (ahora + timedelta(days=1)).date().isoformat()
    # Solo las citas ya aprobadas por el profesor: una pendiente todavia puede no realizarse
    citas = sql("SELECT * FROM (" + CITAS + ") c JOIN (SELECT id AS pid, nombre AS paciente, telefono FROM pacientes"
                " WHERE coalesce(telefono, '') <> '' AND eliminado_en IS NULL AND finalizado_en IS NULL) p"
                " ON p.pid = c.paciente_id WHERE c.fecha = %s AND c.estado = 'programada' AND c.aprobado_en IS NOT NULL"
                " AND c.recordada_en IS NULL ORDER BY c.inicio", manana).fetchall()
    enviados = 0
    for c in citas:
        try:
            enviar_whatsapp(c["telefono"], [c["paciente"], dmy(c["fecha"]), c["hora"], c["sala"]],
                            c["id"])
        except Exception as e:  # noqa: BLE001 - uno que falla no detiene a los demas; la proxima hora se reintenta
            print(f"No se pudo enviar el recordatorio de la cita {c['id']}: {e}")
            continue
        sql("UPDATE citas SET recordada_en = now() WHERE id = %s", c["id"])
        anotar_sistema(f"Recordatorio automático por WhatsApp a {c['paciente']} de su cita del {dmy(c['fecha'])} a las {c['hora']}")
        enviados += 1
    print(f"Recordatorios enviados: {enviados} de {len(citas)}.")


# RF12: el bot de WhatsApp. Ademas de los botones del recordatorio, contesta lo que escriba el paciente con un
# menu numerado. Solo trabaja con las citas que ya agendo el estudiante: no agenda citas nuevas
UBICACION = os.environ.get("CLINICA_UBICACION") or (
    "Clínicas Odontológicas de la Universidad Americana (UAM), Managua. Se atiende de lunes a sábado, en los turnos"
    " de su estudiante. Le pedimos llegar 10 minutos antes de su cita.")
MENU_BOT = ("Responda con un número:\n1. Ver mis próximas citas\n2. Confirmar mi próxima cita\n"
            "3. No podré asistir a mi próxima cita\n4. Ubicación y horario\n5. Que mi estudiante me contacte")
NO_REGISTRADO = ("Hola. Este número no está registrado en las Clínicas Odontológicas de la UAM. Si es paciente,"
                 " pídale a su estudiante que anote este número en su expediente.")

def cuando_es(c, con_paciente=False):
    texto = f"{dmy(c['fecha'])} a las {c['hora']} en la {c['sala']}"
    return texto + (f" ({c['paciente']})" if con_paciente else "")

def cita_por_whatsapp(cita_id, telefono, estados):
    """La cita, si es del paciente de ese telefono, esta en uno de esos estados y no ha pasado."""
    return sql("SELECT c.*, p.nombre AS paciente FROM (" + CITAS + ") c JOIN pacientes p ON p.id = c.paciente_id"
               " WHERE c.id = %s AND '505' || p.telefono = %s AND c.estado = ANY(%s) AND c.fecha >= %s"
               " AND p.eliminado_en IS NULL", cita_id, telefono, list(estados), hoy()).fetchone()

def confirmar_por_whatsapp(c):
    if not c["aprobado_en"]:  # la base no deja confirmar una cita que el profesor todavia no aprueba
        return (f"Su cita del {cuando_es(c)} todavía espera la aprobación del profesor. Cuando la apruebe se la"
                " recordaremos por aquí.")
    sql("UPDATE citas SET estado = 'confirmada' WHERE id = %s", c["id"])
    anotar_sistema(f"{c['paciente']} confirmó su cita del {cuando_es(c)} por WhatsApp")
    return f"Gracias. Su cita del {cuando_es(c)} quedó confirmada."

def cancelar_por_whatsapp(c):
    sql("UPDATE citas SET estado = 'cancelada', motivo_cancelacion = %s WHERE id = %s",
        "El paciente avisó por WhatsApp que no podrá asistir", c["id"])
    anotar_sistema(f"{c['paciente']} avisó que no podrá asistir a su cita del {cuando_es(c)} por WhatsApp")
    return f"Gracias por avisar. Su cita del {cuando_es(c)} quedó cancelada; su estudiante le contactará para darle otra."

def preguntar_whatsapp(telefono, texto, botones):
    """Mensaje con botones de respuesta (hasta 3, de hasta 20 letras). Va dentro de las 24 h, sin plantilla."""
    return _a_whatsapp({"messaging_product": "whatsapp", "to": telefono, "type": "interactive", "interactive": {
        "type": "button", "body": {"text": texto},
        "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t}} for i, t in botones]}}})

def atender_whatsapp(m):
    """Lo que contesta el bot a un mensaje del paciente. Devuelve el texto, o None si no hay que contestar."""
    de = m.get("from", "")
    # Lo que toco: un boton del recordatorio (plantilla) o uno de los que manda el bot (interactivo)
    boton = ((m.get("button") or {}).get("payload")
             or ((m.get("interactive") or {}).get("button_reply") or {}).get("id") or "")
    accion, _, cita_id = boton.partition(":")
    if accion in ("asistira", "no_asistira", "cancelar") and cita_id.isdigit():
        # Los botones del recordatorio solo valen mientras la cita esta Programada; el bot tambien cancela confirmadas
        c = cita_por_whatsapp(int(cita_id), de, ACTIVAS if accion == "cancelar" else ("programada",))
        if not c:
            return None
        return confirmar_por_whatsapp(c) if accion == "asistira" else cancelar_por_whatsapp(c)
    # Un mismo telefono puede ser de varios pacientes (una madre y sus hijos): se atienden todos juntos
    suyos = sql("SELECT id, nombre, estudiante_id FROM pacientes WHERE telefono <> '' AND '505' || telefono = %s"
                " AND eliminado_en IS NULL AND finalizado_en IS NULL ORDER BY id", de).fetchall()
    if not suyos:
        return NO_REGISTRADO
    varios = len(suyos) > 1
    escrito = (m.get("text") or {}).get("body", "") if m.get("type") == "text" else ""
    opcion = re.match(r"\s*([1-5])\b", escrito)
    opcion = opcion.group(1) if opcion else ""
    if not opcion:  # un saludo, una pregunta, un audio o el boton "No, mantenerla": se muestra el menu
        saludo = "Hola" if varios else f"Hola, {suyos[0]['nombre'].split()[0]}"
        return f"{saludo}. Soy el asistente de las Clínicas Odontológicas de la UAM.\n{MENU_BOT}"
    if opcion == "4":
        return UBICACION
    if opcion == "5":
        for p in suyos:  # uno pendiente por paciente: escribir 5 varias veces no lo repite
            if sql("INSERT INTO pedidos_contacto (paciente_id) SELECT %s WHERE NOT EXISTS (SELECT 1 FROM"
                   " pedidos_contacto WHERE paciente_id = %s AND atendido_en IS NULL) RETURNING id",
                   p["id"], p["id"]).fetchone():
                anotar_sistema(f"{p['nombre']} pidió por WhatsApp que su estudiante le contacte")
        quienes = ", ".join(sorted({nombre_de(p["estudiante_id"]) for p in suyos}))
        return f"Listo. Le avisamos a su estudiante ({quienes}) para que le contacte."
    proximas = sql("SELECT c.*, p.nombre AS paciente FROM (" + CITAS + ") c JOIN pacientes p ON p.id = c.paciente_id"
                   " WHERE c.paciente_id = ANY(%s) AND c.estado = ANY(%s) AND c.fecha >= %s ORDER BY c.inicio",
                   [p["id"] for p in suyos], list(ACTIVAS), hoy()).fetchall()
    if not proximas:
        return ("No tiene citas próximas. Si necesita una, responda 5 y su estudiante le contactará.\n" + MENU_BOT)
    if opcion == "1":
        return "Sus próximas citas:\n" + "\n".join(f"- {cuando_es(c, varios)}: {ESTADOS_CITA[c['estado']].lower()}"
                                                   for c in proximas[:5])
    c = proximas[0]
    if opcion == "2":
        if c["estado"] == "confirmada":
            return f"Su cita del {cuando_es(c, varios)} ya está confirmada. ¡Le esperamos!"
        return confirmar_por_whatsapp(c)
    # Opcion 3: antes de cancelar se pregunta, con botones
    preguntar_whatsapp(de, f"¿Seguro que no podrá asistir a su cita del {cuando_es(c, varios)}?",
                       [(f"cancelar:{c['id']}", "Sí, cancelarla"), ("menu", "No, mantenerla")])
    return None


@app.route("/whatsapp", methods=["GET", "POST"])
def whatsapp_respuestas():
    """RF12: Meta avisa aqui de cada mensaje que el paciente le escribe al numero de la clinica: los botones del
    recordatorio y lo que conversa con el bot (ver atender_whatsapp)."""
    if request.method == "GET":  # Meta comprueba la direccion una vez, al configurarla
        verificar = os.environ.get("WHATSAPP_VERIFICAR", "")
        if verificar and request.args.get("hub.mode") == "subscribe" and                 hmac.compare_digest(request.args.get("hub.verify_token", ""), verificar):
            return request.args.get("hub.challenge", "")
        abort(403)
    # Solo se acepta lo que firma Meta con el secreto de la app: nadie mas puede confirmar o cancelar citas
    secreto = os.environ.get("WHATSAPP_SECRETO", "").encode()
    firma = "sha256=" + hmac.new(secreto, request.get_data(), hashlib.sha256).hexdigest()
    if not secreto or not hmac.compare_digest(request.headers.get("X-Hub-Signature-256", ""), firma):
        abort(403)
    datos = request.get_json(silent=True) or {}
    mensajes = [m for e in datos.get("entry", []) for c in e.get("changes", [])
                for m in c.get("value", {}).get("messages", [])]
    for m in mensajes:
        try:
            respuesta = atender_whatsapp(m)
            if respuesta:
                responder_whatsapp(m["from"], respuesta)
        except Exception as e:  # noqa: BLE001 - lo que ya se guardo queda; un mensaje que falla no frena a los demas
            app.logger.warning("No se pudo atender un mensaje de WhatsApp: %s", e)
    return "ok"  # siempre 200: si no, Meta reenvia el mismo mensaje


@app.route("/contactos/<int:kid>", methods=["POST"])
def contacto_atendido(kid):
    """RF12: el estudiante marca que ya contacto al paciente que lo pidio por WhatsApp."""
    yo = quien()[0]
    fila = sql("UPDATE pedidos_contacto k SET atendido_en = now(), atendido_por_id = %s FROM pacientes p WHERE k.id = %s"
               " AND p.id = k.paciente_id AND p.estudiante_id = %s AND k.atendido_en IS NULL RETURNING p.nombre",
               yo, kid, yo).fetchone()
    if fila:
        anotar(f"Contactó a {fila['nombre']}, que lo pidió por WhatsApp")
    return redirect(url_for("menu"))

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
        datos = archivo.read(IMAGEN_MAXIMA + 1)
        if len(datos) > IMAGEN_MAXIMA:
            return None
        with Image.open(io.BytesIO(datos)) as imagen:
            if imagen.format not in ("JPEG", "MPO", "PNG"):  # MPO: JPEG de algunas camaras
                return None
            imagen = ImageOps.exif_transpose(imagen).convert("RGB")  # el celular guarda la foto girada
            imagen.thumbnail((FOTO_LADO, FOTO_LADO))
            salida = io.BytesIO()
            imagen.save(salida, "JPEG", quality=85)
            return salida.getvalue()
    except (OSError, ValueError, AttributeError, Image.DecompressionBombError):  # sin archivo o no es imagen
        return None

# Las fotos presenciales y las de los recibos no van dentro de la base de datos, que crecia con cada imagen:
# se suben a un almacenamiento de archivos compatible con S3 (por ejemplo el Object Storage de Neon,
# Cloudflare R2 o AWS S3) y la base guarda solo su direccion, en la columna url. El bucket es privado: una foto
# se ve solo a traves del sistema, que revisa antes quien la pide (RNF06), y el enlace que da vence en 5 min.
# Sin ALMACEN_BUCKET (en la computadora de desarrollo y en las pruebas) los archivos van a una carpeta local.
ALMACEN = {c: os.environ.get(f"ALMACEN_{c.upper()}", "").strip() for c in ("bucket", "endpoint", "id", "clave", "region")}
CARPETA_LOCAL = Path(os.environ.get("ALMACEN_CARPETA") or Path(__file__).parent / "archivos")
_cliente = []

def s3():
    if not _cliente:
        _cliente.append(boto3.client(
            "s3", endpoint_url=ALMACEN["endpoint"] or None, region_name=ALMACEN["region"] or "auto",
            aws_access_key_id=ALMACEN["id"] or None, aws_secret_access_key=ALMACEN["clave"] or None,
            config=Config(signature_version="s3v4")))
    return _cliente[0]

def guardar_archivo(carpeta, datos):
    """Sube la imagen JPEG y devuelve la direccion que se guarda en la base: s3://bucket/... o local:..."""
    nombre = f"{carpeta}/{secrets.token_hex(16)}.jpg"  # imposible de adivinar
    if ALMACEN["bucket"]:
        s3().put_object(Bucket=ALMACEN["bucket"], Key=nombre, Body=datos, ContentType="image/jpeg")
        return f"s3://{ALMACEN['bucket']}/{nombre}"
    ruta = CARPETA_LOCAL / nombre
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(datos)
    return f"local:{nombre}"

def ruta_local(url):
    ruta = (CARPETA_LOCAL / url.removeprefix("local:")).resolve()
    return ruta if ruta.is_relative_to(CARPETA_LOCAL.resolve()) else None

def ver_archivo(url):
    """La respuesta que muestra la imagen. Se llama solo despues de revisar que el usuario puede verla."""
    if url.startswith("s3://"):
        bucket, _, clave = url.removeprefix("s3://").partition("/")
        return redirect(s3().generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": clave},
                                                    ExpiresIn=300))
    ruta = ruta_local(url) if url.startswith("local:") else None
    if not ruta or not ruta.is_file():
        abort(404)
    return send_file(ruta, mimetype="image/jpeg", max_age=0)

def borrar_archivos(urls):
    """Borra los archivos de filas que se borraron; si alguno falla, la base ya no lo usa y no estorba."""
    for url in urls:
        try:
            if url.startswith("s3://"):
                bucket, _, clave = url.removeprefix("s3://").partition("/")
                s3().delete_object(Bucket=bucket, Key=clave)
            elif (ruta := ruta_local(url)) and ruta.is_file():
                ruta.unlink()
        except Exception:  # noqa: BLE001 - un archivo que no se pudo borrar no detiene la operacion
            app.logger.warning("No se pudo borrar el archivo %s", url)

def cuentas():
    """id de la cuenta -> su fila (id, cif, nombre, rol, activa, clase_id). Se lee una vez por peticion."""
    # ponytail: trae todas las cuentas cada vez; filtrar por id si llegan a ser miles
    if "cuentas" not in g:
        g.cuentas = {f["id"]: f for f in sql("SELECT id, cif, nombre, rol, activa, clase_id FROM cuentas").fetchall()}
    return g.cuentas

def por_cif(cif):
    """La cuenta con ese CIF; None si no existe."""
    return next((c for c in cuentas().values() if c["cif"] == cif), None)

def quien():
    """(id, nombre, rol) de quien entro; None si no hay sesion o si la Facultad desactivo la cuenta."""
    c = cuentas().get(session.get("cuenta"))
    return (c["id"], c["nombre"], c["rol"]) if c and c["activa"] else None

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

def anotar(accion, cuenta_id=None):
    """Bitacora: deja constancia de lo que acaba de hacer el usuario (o la cuenta indicada) y desde qué
    lugar aproximado. Guarda tambien el CIF tal como era, por si la cuenta cambia despues. La IP solo se usa
    para saber el lugar; no se guarda."""
    cid = cuenta_id or quien()[0]
    sql("INSERT INTO bitacora (cuenta_id, cif_registrado, accion, lugar) VALUES (%s, %s, %s, %s)",
        cid, cuentas()[cid]["cif"], accion, lugar_de(ip_del_usuario()))

def comun():
    """Datos que necesita la plantilla base en todas las paginas."""
    yo, nombre, rol = quien()
    if rol == "facultad":  # la Facultad ve en la pestana cuantas clinicas fallan y cuantos materiales se acaban
        avisos = sql("SELECT (SELECT count(*) FROM reportes WHERE resuelto_en IS NULL) AS salas,"
                     " (SELECT count(*) FROM existencias WHERE reponer) AS inventario,"
                     " (SELECT count(*) FROM solicitudes WHERE estado = 'pendiente') AS solicitudes").fetchone()
    elif rol == "profesor":  # y el profesor, lo que espera su firma o su aprobacion, solo de sus estudiantes
        avisos = sql("SELECT (SELECT count(*) FROM recibos WHERE firmado_por_id IS NULL AND anulado_en IS NULL"
                     " AND es_profesor_de(%s, estudiante_id)) AS control,"
                     " (SELECT count(*) FROM pacientes WHERE aprobado_en IS NULL AND rechazo = ''"
                     " AND finalizado_en IS NULL AND eliminado_en IS NULL AND es_profesor_de(%s, estudiante_id)) AS pacientes,"
                     " (SELECT count(*) FROM citas WHERE aprobado_en IS NULL AND estado = 'programada' AND inicio > now()"
                     " AND es_profesor_de(%s, estudiante_id)) AS citas", yo, yo, yo).fetchone()
    else:  # y el estudiante, cuantos de sus pacientes rechazo el profesor (RF04: el aviso al estudiante)
        # y cuantos pacientes pidieron por WhatsApp que les contacte (RF12)
        avisos = sql("SELECT count(*) AS pacientes, (SELECT count(*) FROM pedidos_contacto k JOIN pacientes p ON"
                     " p.id = k.paciente_id WHERE k.atendido_en IS NULL AND p.estudiante_id = %s AND p.eliminado_en IS NULL)"
                     " AS menu FROM pacientes WHERE estudiante_id = %s AND rechazo <> ''"
                     " AND finalizado_en IS NULL AND eliminado_en IS NULL", yo, yo).fetchone()
    pestanas = [(f"{t} ({avisos[e]})" if avisos.get(e) else t, e) for t, e in MENUS[rol]
                if e != "comentarios" or COMENTARIOS]
    return {"yo": yo, "cif": cuentas()[yo]["cif"], "nombre": nombre, "rol": ROLES[rol], "es_rol": rol,
            "pestanas": pestanas, "fallas": avisos.get("salas", 0), "bajos": avisos.get("inventario", 0),
            "solicitudes_pendientes": avisos.get("solicitudes", 0)}

def visibles(alias="pacientes"):
    """Condicion SQL (y sus argumentos) de los pacientes que el usuario puede ver. El estudiante, los suyos;
    el profesor, los de los estudiantes de sus clases (RF06/RNF06); la Facultad, todos."""
    yo, _, rol = quien()
    if rol == "estudiante":
        return f"{alias}.estudiante_id = %s", [yo]
    if rol == "profesor":
        return f"es_profesor_de(%s, {alias}.estudiante_id)", [yo]
    return "true", []

def es_profesor_de(profesor_id, estudiante_id):
    """True si el estudiante esta en una clase de ese profesor (la misma regla de la base)."""
    return bool(sql("SELECT es_profesor_de(%s, %s) AS si", profesor_id, estudiante_id).fetchone()["si"])

def paciente(pid):
    """El paciente, solo si el usuario puede verlo: cada estudiante ve unicamente los suyos.
    Si la Facultad le dio de alta, solo ella puede cambiar algo."""
    _, _, rol = quien()
    condicion, args = visibles("p")
    p = sql("SELECT p.*, p.aprobado_en IS NOT NULL AS aprobado FROM pacientes p WHERE p.id = %s"
            " AND p.eliminado_en IS NULL AND " + condicion, pid, *args).fetchone()
    if p and p["finalizado_en"] and rol != "facultad" and request.method == "POST":
        flash(f"{p['nombre']} ya recibió el alta: su expediente ya no se puede cambiar.", "error")
        return None
    return p

def mis_pacientes(buscar=""):
    """Los pacientes que el usuario puede ver; con `buscar`, solo los que coinciden en parte con el nombre, la
    cédula (con o sin guiones) o el número de expediente (RF06). Trae cuántas veces no llegó a una cita."""
    condicion, args = visibles("p")
    texto = "%" + re.sub(r"([\\%_])", r"\\\1", buscar.strip()) + "%"
    cedula = "%" + sin_signos(buscar) + "%" if sin_signos(buscar) else ""
    return sql("SELECT p.id, p.nombre, p.cedula, p.telefono, p.expediente, p.razon, p.estudiante_id,"
               " p.aprobado_en IS NOT NULL AS aprobado, p.rechazo, p.firma <> '' AS firma, p.finalizado_en,"
               " (SELECT count(*) FROM citas c WHERE c.paciente_id = p.id AND c.estado = 'no_asistio') AS inasistencias"
               " FROM pacientes p WHERE p.eliminado_en IS NULL AND " + condicion +
               " AND (%s = '' OR p.nombre ILIKE %s OR p.expediente ILIKE %s"
               " OR regexp_replace(upper(p.cedula), '[^0-9A-Z]', '', 'g') LIKE %s) ORDER BY p.id",
               *args, buscar.strip(), texto, texto, cedula).fetchall()

def nombre_de(cuenta_id):
    return cuentas()[cuenta_id]["nombre"] if cuenta_id in cuentas() else "sin asignar"

def todas_las_salas():
    """Las clinicas, ordenadas por sala y por su número (así salen en su lugar del plano), y si tienen una
    falla sin solucionar, qué es y quién la reportó."""
    return sql("SELECT s.*, r.id AS reporte_id, r.descripcion AS motivo, r.tipo, r.reportado_por_id AS reporto,"
               " to_char(r.creado_en AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS reportada"
               " FROM salas s LEFT JOIN reportes r ON r.sala_id = s.id AND r.resuelto_en IS NULL"
               " ORDER BY s.en_sala, substring(s.nombre FROM '[0-9]+')::numeric NULLS LAST, s.id").fetchall()


@app.before_request
def exigir_sesion():
    if request.endpoint not in ("entrar", "registro", "static", "whatsapp_respuestas", "privacidad") and quien() is None:
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
            cuenta = sql("SELECT id, clave_hash, activa, ceil(extract(epoch FROM bloqueada_hasta - now()) / 60) AS espera"
                         " FROM cuentas WHERE cif = %s", cif).fetchone()
            if cuenta and cuenta["espera"] and cuenta["espera"] > 0:
                # Mientras esta bloqueada ni se mira la clave: asi no se puede seguir probando
                datos["aviso"] = f"Cuenta bloqueada por intentos fallidos. Intente de nuevo en {cuenta['espera']:.0f} min."
            elif cuenta and (correcta := clave_correcta(cuenta["clave_hash"], clave)) and not cuenta["activa"]:
                datos["aviso"] = "Esta cuenta está desactivada. Consulte con la Facultad."
            elif cuenta and correcta:
                sql("UPDATE cuentas SET fallos = 0 WHERE id = %s", cuenta["id"])
                session.permanent = True  # cuenta los 15 min desde la ultima pagina que se abrio
                session["cuenta"] = cuenta["id"]
                anotar("Entró al sistema")
                destino = session.pop("codigo_clase", None)  # venia de escanear el QR de una clase
                return redirect(url_for("registro", codigo=destino) if destino else url_for("menu"))
            else:
                datos["aviso"] = "CIF o contraseña incorrectos."
                if cuenta and sql("UPDATE cuentas SET fallos = fallos + 1 WHERE id = %s RETURNING fallos",
                                  cuenta["id"]).fetchone()["fallos"] >= FALLOS_MAXIMOS:
                    sql("UPDATE cuentas SET fallos = 0, bloqueada_hasta = now() + %s * interval '1 minute' "
                        "WHERE id = %s", MINUTOS_BLOQUEO, cuenta["id"])
                    anotar(f"Cuenta bloqueada {MINUTOS_BLOQUEO} min por {FALLOS_MAXIMOS} contraseñas equivocadas",
                           cuenta["id"])
                    datos["aviso"] = (f"Demasiados intentos fallidos. La cuenta queda bloqueada "
                                      f"{MINUTOS_BLOQUEO} min.")
    return render_template("entrar.html", **datos)


def citas_de(condicion, *args, orden="c.inicio"):
    """Citas (con fecha, hora y clinica a la vista) y el nombre de su paciente, de los pacientes que se ven."""
    return sql("SELECT c.*, p.nombre AS paciente, p.telefono, p.estudiante_id AS a_cargo FROM (" + CITAS + ") c"
               " JOIN pacientes p ON p.id = c.paciente_id WHERE p.eliminado_en IS NULL AND " + condicion
               + " ORDER BY " + orden, *args).fetchall()

def clase_de(cuenta_id):
    """La clase actual del estudiante con el nombre de su profesor; None si todavia no esta en ninguna."""
    return sql("SELECT k.*, p.nombre AS profesor FROM cuentas e JOIN clases k ON k.id = e.clase_id"
               " JOIN cuentas p ON p.id = k.profesor_id WHERE e.id = %s", cuenta_id).fetchone()


@app.route("/menu")
def menu():
    yo, _, rol = quien()
    condicion, args = visibles("p")
    # Al profesor le aparecen los odontogramas, pacientes y citas que esperan su revision
    revisar = sql("SELECT p.id, p.nombre, p.estudiante_id FROM pacientes p WHERE p.odonto_revision = 'pedida'"
                  " AND p.eliminado_en IS NULL AND " + condicion + " ORDER BY p.id", *args).fetchall() if rol == "profesor" else []
    rechazados = sql("SELECT id, nombre, rechazo, rechazado_por_id FROM pacientes WHERE estudiante_id = %s"
                     " AND rechazo <> '' AND finalizado_en IS NULL AND eliminado_en IS NULL ORDER BY id",
                     yo).fetchall() if rol == "estudiante" else []
    por_aprobar = sql("SELECT p.id, p.nombre, p.estudiante_id FROM pacientes p WHERE p.aprobado_en IS NULL AND p.rechazo = ''"
                      " AND p.finalizado_en IS NULL AND p.eliminado_en IS NULL AND " + condicion + " ORDER BY p.id",
                      *args).fetchall() if rol == "profesor" else []
    citas_por_aprobar = citas_de("c.aprobado_en IS NULL AND c.estado = 'programada' AND c.inicio > now() AND "
                                 + condicion, *args) if rol == "profesor" else []
    # RF13: la jornada de hoy segun el rol. El estudiante ve sus citas; el profesor y la Facultad, las que ven
    manana = (datetime.now(MANAGUA) + timedelta(days=1)).date().isoformat()
    citas_hoy = citas_de("c.fecha = %s AND c.estado <> 'cancelada' AND " + condicion, hoy(), *args)
    clinicas = sql("SELECT count(*) FILTER (WHERE habilitada) AS disponibles, count(*) FILTER (WHERE NOT habilitada)"
                   " AS no_disponibles FROM salas").fetchone()
    return render_template("menu.html", **comun(), que_hace=QUE_HACE[rol], revisar=revisar, rechazados=rechazados,
                           nombre_de=nombre_de, citas_hoy=citas_hoy, estados_cita=ESTADOS_CITA,
                           turnos=turnos_de(yo) if rol == "estudiante" else [], dias=DIAS,
                           clase=clase_de(yo) if rol == "estudiante" else None,
                           # RF12: las citas de manana que el paciente todavia no confirma, para recordarselas
                           sin_confirmar=citas_de("c.fecha = %s AND c.estado = 'programada' AND c.aprobado_en IS NOT NULL"
                                                  " AND " + condicion, manana, *args) if rol == "estudiante" else [],
                           por_aprobar=por_aprobar, citas_por_aprobar=citas_por_aprobar, clinicas=clinicas,
                           # RF12: los pacientes que pidieron por WhatsApp que el estudiante les contacte
                           contactar=sql("SELECT k.id, k.pedido_en, p.id AS paciente_id, p.nombre, p.telefono FROM"
                                         " pedidos_contacto k JOIN pacientes p ON p.id = k.paciente_id WHERE"
                                         " k.atendido_en IS NULL AND p.estudiante_id = %s AND p.eliminado_en IS NULL"
                                         " ORDER BY k.pedido_en", yo).fetchall() if rol == "estudiante" else [])


@app.route("/pacientes", methods=["GET", "POST"])
def pacientes():
    yo, _, rol = quien()
    form = {}
    if request.method == "POST":
        accion = request.form.get("accion")
        if accion == "registrar" and rol == "estudiante":
            form = {c: request.form.get(c, "").strip() for c in ("nombre", "cedula", "telefono", "expediente",
                    "nacimiento", "sexo", "direccion", "emergencia_nombre", "emergencia_telefono")}
            if error := error_de_paciente(form) or error_de_historia(form):
                flash(error, "error")
            else:
                with bd().transaction():
                    pid = sql("SELECT nextval(pg_get_serial_sequence('pacientes', 'id')) AS id").fetchone()["id"]
                    # RF03: si no trae numero de expediente fisico, se le da uno correlativo
                    sql("INSERT INTO pacientes (id, cedula, expediente, nombre, nacimiento, sexo, telefono, estudiante_id)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", pid, form["cedula"],
                        form["expediente"] or f"EXP-{pid:05d}", form["nombre"], form["nacimiento"] or None,
                        form["sexo"] or None, form["telefono"], yo)
                    sql("INSERT INTO historias (paciente_id, direccion, emergencia_nombre, emergencia_telefono,"
                        " actualizada_por_id) VALUES (%s, %s, %s, %s, %s)", pid, form["direccion"],
                        form["emergencia_nombre"], form["emergencia_telefono"], yo)
                    anotar(f"Registró al paciente {form['nombre']} ({form['cedula']})")
                # Paso siguiente: la razon por la que esta el paciente
                return redirect(url_for("razon", pid=pid))
        elif accion in ("aprobar", "rechazar") and rol == "profesor":  # solo de los estudiantes de sus clases
            p = paciente(request.form.get("id", type=int))
            observacion = request.form.get("observacion", "").strip()
            if not p:
                flash("Ese paciente no existe.", "error")
            elif accion == "rechazar" and not observacion:
                flash("Para rechazar al paciente escriba la observación para el estudiante.", "error")
            elif accion == "rechazar" and len(observacion) > MOTIVO_MAXIMO:
                flash(f"La observación puede tener hasta {MOTIVO_MAXIMO} caracteres.", "error")
            elif accion == "rechazar":
                sql("UPDATE pacientes SET aprobado_por_id = NULL, aprobado_en = NULL, rechazo = %s,"
                    " rechazado_por_id = %s, rechazado_en = now() WHERE id = %s", observacion, yo, p["id"])
                anotar(f"Rechazó a {p['nombre']}: {observacion}")
                flash(f"{p['nombre']} rechazado. El estudiante verá la observación.", "ok")
            else:
                # Aprobar o quitar la aprobación: queda quién lo decidió y cuándo (RF04)
                aprobado = request.form.get("aprobado") == "1"
                sql("UPDATE pacientes SET aprobado_por_id = %s, aprobado_en = CASE WHEN %s THEN now() END, rechazo = '',"
                    " rechazado_por_id = NULL, rechazado_en = NULL WHERE id = %s",
                    yo if aprobado else None, aprobado, p["id"])
                anotar(("Aprobó" if aprobado else "Retiró la aprobación de") + f" a {p['nombre']}")
                flash("Paciente aprobado." if aprobado else "Aprobación retirada.", "ok")
        elif accion == "restaurar" and rol == "facultad":
            r = sql("UPDATE pacientes SET eliminado_en = NULL, eliminado_por_id = NULL, eliminado_motivo = ''"
                    " WHERE id = %s AND eliminado_en IS NOT NULL RETURNING nombre", request.form.get("id", type=int)).fetchone()
            if r:
                anotar(f"Restauró al paciente {r['nombre']}")
                flash(f"{r['nombre']} restaurado con todo su expediente.", "ok")
    buscar = request.args.get("buscar", "").strip()
    eliminados = sql("SELECT id, nombre, cedula, eliminado_motivo, eliminado_por_id, to_char(eliminado_en AT TIME ZONE"
                     " 'America/Managua', 'DD/MM/YYYY') AS cuando FROM pacientes WHERE eliminado_en IS NOT NULL"
                     " ORDER BY eliminado_en DESC").fetchall() if rol == "facultad" else []
    return render_template("pacientes.html", **comun(), pacientes=mis_pacientes(buscar), buscar=buscar, form=form,
                           nombre_de=nombre_de, sexos=SEXOS, eliminados=eliminados,
                           sin_clase=rol == "estudiante" and not clase_de(yo))


def crear_cita(p, rango, sala_id, razon):
    """Agenda la cita del paciente (ya revisada con error_de_cita). Si la agenda el estudiante queda esperando la
    aprobacion de su profesor; si la agenda el profesor, la base la deja aprobada. Devuelve la cita."""
    yo = quien()[0]
    c = sql("INSERT INTO citas (paciente_id, sala_id, estudiante_id, creado_por_id, inicio, fin, razon)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id", p["id"], sala_id, p["estudiante_id"], yo,
            rango[0], rango[1], razon).fetchone()
    c = sql(CITAS + " WHERE c.id = %s", c["id"]).fetchone()
    anotar(f"Agendó cita de {p['nombre']} el {dmy(c['fecha'])} a las {c['hora']}"
           + ("" if c["aprobado_en"] else " (espera la aprobación del profesor)"))
    if c["aprobado_en"]:  # al paciente se le avisa cuando la cita ya esta aprobada
        avisar_cita(p, c["fecha"], c["hora"], c["sala"], "agendada")
    return c

def datos_de_cita(form):
    """(rango, sala_id) de lo que se escribio en el formulario de una cita."""
    minutos = form.get("minutos", type=int) or 60
    return rango_cita(form.get("fecha", "").strip(), form.get("hora", "").strip(), minutos), form.get("sala", type=int)


@app.route("/pacientes/<int:pid>/razon", methods=["GET", "POST"])
def razon(pid):
    """Paso 2 del registro: por qué está el paciente y, si ya se sabe, su primera cita."""
    p = paciente(pid)
    if not p or quien()[2] != "estudiante":
        return redirect(url_for("pacientes"))
    if request.method == "POST":
        razon = request.form.get("razon", "").strip()
        rango, sala_id = datos_de_cita(request.form)
        # La primera cita es opcional, pero si se empieza a llenar se valida igual que en Citas
        con_cita = bool(request.form.get("fecha") or request.form.get("hora"))
        error = con_cita and error_de_cita(rango, sala_id, pid, p["estudiante_id"])
        p["razon"] = razon  # si hay error, el formulario vuelve con lo que se escribio
        if not razon:
            flash("Escriba la razón por la que viene el paciente.", "error")
        elif error:
            flash(error, "error")
        else:
            sql("UPDATE pacientes SET razon = %s WHERE id = %s", razon, pid)
            anotar(f"Anotó la razón de {p['nombre']}: {razon}")
            if con_cita:
                crear_cita(p, rango, sala_id, request.form.get("razon_cita", "").strip() or razon)
                flash("Razón guardada y cita agendada; espera la aprobación del profesor.", "ok")
            else:
                flash("Razón de la consulta guardada.", "ok")
            return redirect(url_for("expediente", pid=pid))
    return render_template("razon.html", **comun(), p=p, hoy=hoy(), salas=todas_las_salas(), duraciones=DURACIONES,
                           apertura=APERTURA, cierre=CIERRE)


@app.route("/pacientes/<int:pid>/editar", methods=["GET", "POST"])
def editar(pid):
    """El estudiante a cargo corrige los datos del paciente si se equivocó al registrarlo."""
    p = paciente(pid)
    if not p or quien()[2] != "estudiante":
        return redirect(url_for("pacientes"))
    if request.method == "POST":
        form = {c: request.form.get(c, "").strip() for c in ("nombre", "cedula", "telefono", "expediente")}
        form["expediente"] = form["expediente"] or p["expediente"]  # el expediente nunca queda vacio
        p.update(form)  # si hay error, el formulario vuelve con lo que se escribio
        if error := error_de_paciente(form, pid):
            flash(error, "error")
        else:
            sql("UPDATE pacientes SET nombre = %s, cedula = %s, telefono = %s, expediente = %s WHERE id = %s",
                form["nombre"], form["cedula"], form["telefono"], form["expediente"], pid)
            anotar(f"Corrigió los datos de {form['nombre']} ({form['cedula']})")
            flash("Datos del paciente guardados.", "ok")
            return redirect(url_for("expediente", pid=pid))
    return render_template("editar.html", **comun(), p=p)


def trabajo_de(p):
    """Quién tuvo a cargo al paciente y desde cuándo, con el resumen que dejó al pasarlo a otro estudiante."""
    periodos, desde = [], p["creado_en"]
    for t in sql("SELECT * FROM traspasos WHERE paciente_id = %s ORDER BY id", p["id"]).fetchall():
        periodos.append({"estudiante_id": t["de_id"], "desde": desde, "hasta": t["creado_en"], "resumen": t["resumen"]})
        desde = t["creado_en"]
    periodos.append({"estudiante_id": p["estudiante_id"], "desde": desde, "hasta": None, "resumen": ""})
    return periodos


@app.route("/pacientes/<int:pid>")
def expediente(pid):
    p = paciente(pid)
    if not p:
        return redirect(url_for("pacientes"))
    p["trabajo"] = trabajo_de(p)
    citas = sql(CITAS + " WHERE c.paciente_id = %s ORDER BY c.inicio", pid).fetchall()
    h = historia_de(pid)
    return render_template("expediente.html", **comun(), p=p, nombre_de=nombre_de, revisiones=REVISIONES, citas=citas,
                           color_cita=COLOR_CITA, estados_cita=ESTADOS_CITA,
                           inasistencias=sum(c["estado"] == "no_asistio" for c in citas),
                           h=h, alertas=alertas_de(h), avance=avance_de(pid))


def historia_de(pid):
    """La historia clinica con la fecha de nacimiento y el sexo del paciente, lista para el formulario."""
    h = sql("SELECT h.*, p.nacimiento, p.sexo FROM pacientes p LEFT JOIN historias h ON h.paciente_id = p.id"
            " WHERE p.id = %s", pid).fetchone() or {}
    h = {c: ("" if v is None else v) for c, v in h.items()}
    h["nacimiento"] = h["nacimiento"].isoformat() if h.get("nacimiento") else ""
    h["completa"] = bool(h.get("completa"))
    return h

def presion_alta(texto):
    """'150/95' -> True. Presion alta: 140 o mas de sistolica, o 90 o mas de diastolica."""
    m = re.fullmatch(r"\s*(\d{2,3})\s*/\s*(\d{2,3})\s*", texto or "")
    return bool(m) and (int(m[1]) >= 140 or int(m[2]) >= 90)

def alertas_de(h):
    """Lo que hay que ver antes de atender al paciente (RF05)."""
    alertas = []
    if (h.get("alergias") or "").strip():
        alertas.append(f"Alergias: {h['alergias']}")
    if presion_alta(h.get("presion")):
        alertas.append(f"Presión arterial alta: {h['presion']}")
    return alertas

def error_de_historia(form):
    """Revisa los datos de la historia clinica que tienen formato; None si estan bien."""
    if form.get("nacimiento"):
        try:
            if datetime.strptime(form["nacimiento"], "%Y-%m-%d").date().isoformat() > hoy():
                return "La fecha de nacimiento no puede ser después de hoy."
        except ValueError:
            return "Elija la fecha de nacimiento en el calendario."
    if form.get("sexo", "") not in ("", *SEXOS):
        return "Elija el sexo de la lista."
    if form.get("emergencia_telefono") and telefono_nica(form["emergencia_telefono"]) is None:
        return "El teléfono de emergencia debe tener 8 dígitos."
    if form.get("emergencia_telefono"):
        form["emergencia_telefono"] = telefono_nica(form["emergencia_telefono"])
    if form.get("presion") and not re.fullmatch(r"\s*\d{2,3}\s*/\s*\d{2,3}\s*", form["presion"]):
        return "Escriba la presión como sistólica/diastólica, por ejemplo 120/80."
    return None

def avance_de(pid):
    """Cuanto del plan esta realizado (RF09): un tratamiento esta realizado si tiene la cita en que se hizo."""
    a = sql("SELECT count(cita_id) AS hechos, count(*) AS total FROM tratamientos WHERE paciente_id = %s",
            pid).fetchone()
    a["porcentaje"] = round(100 * a["hechos"] / a["total"]) if a["total"] else 0
    return a


@app.route("/pacientes/<int:pid>/historia", methods=["GET", "POST"])
def historia(pid):
    """Historia clinica digital (RF05): la llena el estudiante a cargo y se puede guardar a medias para
    seguir en la proxima cita. Quien la cambio y que campos queda en la bitacora (historial de cambios)."""
    p = paciente(pid)
    if not p:
        return redirect(url_for("pacientes"))
    yo, _, rol = quien()
    h = historia_de(pid)
    campos = {**DEL_PACIENTE, **HISTORIA}
    if request.method == "POST" and rol == "estudiante":
        form = {c: request.form.get(c, "").strip() for c in campos}
        if error := error_de_historia(form):
            flash(error, "error")
            h = {**h, **form}
        else:
            cambiados = [campos[c] for c in campos if (str(h.get(c) or "")) != form[c]]
            # Se guarda siempre, para no perder lo escrito; completa solo si se pide y no falta nada
            faltan = [campos[c] for c in HISTORIA_COMPLETA if not form[c]]
            completa = request.form.get("accion") == "completa" and not faltan
            columnas = list(HISTORIA)
            with bd().transaction():
                sql("UPDATE pacientes SET nacimiento = %s, sexo = %s WHERE id = %s",
                    form["nacimiento"] or None, form["sexo"] or None, pid)
                sql("INSERT INTO historias (paciente_id, " + ", ".join(columnas) + ", completa, actualizada_en,"
                    " actualizada_por_id) VALUES (%s, " + ", ".join(["%s"] * len(columnas)) + ", %s, now(), %s)"
                    " ON CONFLICT (paciente_id) DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in columnas)
                    + ", completa = excluded.completa, actualizada_en = now(),"
                    " actualizada_por_id = excluded.actualizada_por_id",
                    pid, *[form[c] for c in columnas], completa, yo)
            if cambiados:
                anotar(f"Actualizó la historia clínica de {p['nombre']}: " + ", ".join(cambiados).lower())
            if completa and not h.get("completa"):
                anotar(f"Marcó como completa la historia clínica de {p['nombre']}")
            elif not completa and h.get("completa"):
                anotar(f"Volvió a borrador la historia clínica de {p['nombre']}")
            if request.form.get("accion") == "completa" and faltan:
                flash("Se guardó como borrador. Para marcarla completa falta: " + ", ".join(faltan).lower()
                      + ". Si no tiene alergias o medicamentos, escriba «Ninguna».", "error")
            elif completa:
                flash("Historia clínica guardada y marcada como completa.", "ok")
            else:
                flash("Borrador guardado. Puede seguir llenándolo en la próxima cita.", "ok")
            return redirect(url_for("historia", pid=pid))
    return render_template("historia.html", **comun(), p=p, h=h, campos=campos, sexos=SEXOS, alertas=alertas_de(h),
                           hoy=hoy(), nombre_de=nombre_de, puede=rol == "estudiante" and not p["finalizado_en"],
                           obligatorios=[campos[c] for c in HISTORIA_COMPLETA])


@app.route("/pacientes/<int:pid>/traspaso", methods=["GET", "POST"])
def traspaso(pid):
    """Cambiar de estudiante: queda el resumen de lo que trabajó el anterior."""
    p = paciente(pid)
    if not p or quien()[2] != "facultad" or p["finalizado_en"]:  # un paciente con alta ya no cambia de estudiante
        return redirect(url_for("pacientes"))
    estudiantes = [(c["id"], c["nombre"]) for c in cuentas().values() if c["rol"] == "estudiante" and c["activa"]]
    if request.method == "POST":
        nuevo = request.form.get("estudiante", type=int)
        resumen = request.form.get("resumen", "").strip()
        if nuevo not in dict(estudiantes) or nuevo == p["estudiante_id"]:
            flash("Elija el estudiante que recibe al paciente.", "error")
        elif not resumen:
            flash("Escriba qué se le hizo al paciente y qué trabajó el estudiante anterior.", "error")
        else:
            with bd().transaction():
                sql("INSERT INTO traspasos (paciente_id, de_id, a_id, resumen, hecho_por_id) VALUES (%s, %s, %s, %s, %s)",
                    pid, p["estudiante_id"], nuevo, resumen, quien()[0])
                sql("UPDATE pacientes SET estudiante_id = %s WHERE id = %s", nuevo, pid)
                anotar(f"Pasó a {p['nombre']} de {nombre_de(p['estudiante_id'])} a {nombre_de(nuevo)}")
            flash(f"{p['nombre']} pasó a {nombre_de(nuevo)}. El trabajo anterior quedó archivado.", "ok")
            return redirect(url_for("expediente", pid=pid))
    return render_template("traspaso.html", **comun(), p=p, estudiantes=estudiantes, nombre_de=nombre_de)


@app.route("/pacientes/<int:pid>/cerrar", methods=["GET", "POST"])
def cerrar(pid):
    """Solo la Facultad: da de alta al paciente, anula el alta o elimina al paciente."""
    p = paciente(pid)
    yo, _, rol = quien()
    if not p or rol != "facultad":
        return redirect(url_for("pacientes"))
    if request.method == "POST":
        accion, texto = request.form.get("accion"), request.form.get("texto", "").strip()
        if accion == "finalizar" and not p["finalizado_en"]:
            if not texto:
                flash("Escriba cómo terminó el tratamiento del paciente.", "error")
            else:
                sql("UPDATE pacientes SET finalizado_en = now(), finalizado_por_id = %s, finalizado_resumen = %s"
                    " WHERE id = %s", yo, texto, pid)
                anotar(f"Dio de alta a {p['nombre']}: {texto}")
                flash(f"{p['nombre']} recibió el alta. Su expediente ya no se puede cambiar.", "ok")
                return redirect(url_for("expediente", pid=pid))
        elif accion == "reabrir" and p["finalizado_en"]:
            sql("UPDATE pacientes SET finalizado_en = NULL, finalizado_por_id = NULL, finalizado_resumen = ''"
                " WHERE id = %s", pid)
            anotar(f"Anuló el alta de {p['nombre']}")
            flash(f"Se anuló el alta de {p['nombre']}.", "ok")
            return redirect(url_for("expediente", pid=pid))
        elif accion == "eliminar":
            # Se pide la cedula para que no se borre a nadie por un clic equivocado
            if sin_signos(request.form.get("cedula")) != sin_signos(p["cedula"]) or not texto:
                flash("Para eliminarlo escriba la cédula del paciente y el motivo.", "error")
            elif any(r["firmado_por_id"] for r in recibos_de(paciente_id=pid)):
                flash(f"{p['nombre']} tiene recibos de pago firmados por el docente: no se puede eliminar."
                      " Si terminó su tratamiento, dele de alta.", "error")
            else:
                # RNF07: no se borra nada; deja de verse y la Facultad lo puede restaurar desde Pacientes.
                # Sus citas que vienen se cancelan para que la clínica quede libre a esa hora
                with bd().transaction():
                    sql("UPDATE citas SET estado = 'cancelada', motivo_cancelacion = 'Se eliminó al paciente'"
                        " WHERE paciente_id = %s AND estado IN ('programada', 'confirmada') AND inicio > now()", pid)
                    sql("UPDATE pacientes SET eliminado_en = now(), eliminado_por_id = %s, eliminado_motivo = %s"
                        " WHERE id = %s", yo, texto, pid)
                    anotar(f"Eliminó al paciente {p['nombre']} ({p['cedula']}): {texto}")
                flash(f"{p['nombre']} fue eliminado. Su expediente se conserva y se puede restaurar.", "ok")
                return redirect(url_for("pacientes"))
        return redirect(url_for("cerrar", pid=pid))
    return render_template("cerrar.html", **comun(), p=p, nombre_de=nombre_de, cuenta=cuenta_de(pid),
                           pendientes=[t for t in plan_de(pid) if not t["cita_id"]])


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
            sql("UPDATE pacientes SET firma = %s, firmado_en = now() WHERE id = %s", firma, pid)
            anotar(f"Recogió la firma del consentimiento informado de {p['nombre']}")
            flash("Consentimiento informado firmado. Ya se puede imprimir.", "ok")
        return redirect(url_for("consentimiento", pid=pid))
    return render_template("consentimiento.html", **comun(), p=p, nombre_de=nombre_de)


@app.route("/citas", methods=["GET", "POST"])
def citas():
    """Las citas de los pacientes que se ven. El estudiante agenda las de sus pacientes y quedan esperando a su
    profesor; el profesor las aprueba (o las rechaza) y también puede agendarlas él, ya aprobadas. La Facultad las ve."""
    yo, _, rol = quien()
    accion = request.form.get("accion")
    if request.method == "POST" and accion in ("cancelar", "estado", "reprogramar", "aprobar", "rechazar"):
        cita = sql(CITAS + " WHERE c.id = %s", request.form.get("id", type=int)).fetchone()
        p = cita and paciente(cita["paciente_id"])
        if p and rol == "estudiante" and accion in ("cancelar", "estado", "reprogramar"):
            cambiar_cita(cita, p, accion)
        elif p and rol == "profesor" and accion in ("aprobar", "rechazar"):
            decidir_cita(cita, p, accion)
        return redirect(url_for("citas"))
    if request.method == "POST" and rol in ("estudiante", "profesor"):
        p = paciente(request.form.get("paciente_id", type=int))
        rango, sala_id = datos_de_cita(request.form)
        razon = request.form.get("razon", "").strip()
        if not (p and razon):
            flash("Elija el paciente, la fecha, la hora, la clínica y la razón de la cita.", "error")
        elif rol == "profesor" and not es_profesor_de(yo, p["estudiante_id"]):
            flash("Solo puede agendar citas de los estudiantes de sus clases.", "error")
        elif error := error_de_cita(rango, sala_id, p["id"], p["estudiante_id"]):
            flash(error, "error")
        else:
            c = crear_cita(p, rango, sala_id, razon)
            flash("Cita agendada." if c["aprobado_en"] else "Cita agendada; espera la aprobación de su profesor.", "ok")
        return redirect(url_for("citas"))
    condicion, args = visibles("p")
    return render_template("citas.html", **comun(), hoy=hoy(), salas=todas_las_salas(), pacientes=mis_pacientes(),
                           citas=citas_de(condicion, *args, orden="c.inicio DESC"), nombre_de=nombre_de,
                           activas=ACTIVAS, color_cita=COLOR_CITA, estados_cita=ESTADOS_CITA, siguiente=SIGUIENTE,
                           duraciones=DURACIONES, apertura=APERTURA, cierre=CIERRE)


def decidir_cita(cita, p, accion):
    """El profesor aprueba la cita que agendó su estudiante (la base revisa que sea de sus clases) o la rechaza,
    que la deja cancelada con el motivo."""
    if cita["aprobado_en"] or cita["estado"] != "programada":
        flash("Esa cita ya no espera aprobación.", "error")
    elif accion == "aprobar":
        sql("UPDATE citas SET aprobado_por_id = %s WHERE id = %s", quien()[0], cita["id"])
        anotar(f"Aprobó la cita de {p['nombre']} del {dmy(cita['fecha'])} a las {cita['hora']}")
        avisar_cita(p, cita["fecha"], cita["hora"], cita["sala"], "agendada")
        flash("Cita aprobada.", "ok")
    elif not (motivo := request.form.get("motivo", "").strip()[:MOTIVO_MAXIMO]):
        flash("Escriba por qué rechaza la cita.", "error")
    else:
        sql("UPDATE citas SET estado = 'cancelada', motivo_cancelacion = %s WHERE id = %s",
            "Rechazada por el profesor: " + motivo, cita["id"])
        anotar(f"Rechazó la cita de {p['nombre']} del {dmy(cita['fecha'])}: {motivo}")
        flash("Cita rechazada; el estudiante verá el motivo.", "ok")


def cambiar_cita(cita, p, accion):
    """El estudiante a cargo confirma, marca como atendida o como no se presentó, reprograma o cancela una cita
    (RF11). Nada se borra: la cita queda en el historial del paciente con su estado."""
    cuando = f"{dmy(cita['fecha'])} a las {cita['hora']}"
    nuevo = "cancelada" if accion == "cancelar" else request.form.get("estado", "")
    if cita["estado"] not in ACTIVAS:
        flash(f"Esa cita ya está {ESTADOS_CITA[cita['estado']].lower()}: ya no se cambia.", "error")
    elif accion == "reprogramar":
        rango, sala_id = datos_de_cita(request.form)
        if cita["fecha"] < hoy():
            flash("Esa cita ya pasó: márquela como atendida o como que no se presentó.", "error")
        elif error := error_de_cita(rango, sala_id, p["id"], cita["estudiante_id"], cita["id"]):
            flash(error, "error")
        else:
            # La cita cambia de hora: vuelve a esperar la aprobación del profesor
            sql("UPDATE citas SET inicio = %s, fin = %s, sala_id = %s, estado = 'programada', recordada_en = NULL,"
                " aprobado_por_id = NULL, aprobado_en = NULL WHERE id = %s", rango[0], rango[1], sala_id, cita["id"])
            anotar(f"Reprogramó la cita de {p['nombre']} del {cuando} al {rango[0]:%d/%m/%Y} a las {rango[0]:%H:%M}")
            flash("Cita reprogramada; espera otra vez la aprobación del profesor.", "ok")
    elif nuevo not in SIGUIENTE[cita["estado"]]:
        flash("Elija qué pasó con la cita.", "error")
    elif nuevo in ("confirmada", "atendida") and not cita["aprobado_en"]:
        flash("Esa cita todavía no la aprueba el profesor: no se puede confirmar ni atender.", "error")
    elif nuevo in ("confirmada", "cancelada") and cita["fecha"] < hoy():
        # Las citas pasadas no se cancelan ni se confirman: son historial
        flash("Esa cita ya pasó: márquela como atendida o como que no se presentó.", "error")
    elif nuevo in ("atendida", "no_asistio") and cita["fecha"] > hoy():
        flash("Una cita se marca como atendida o como que no se presentó el día de la cita o después.", "error")
    elif nuevo == "cancelada" and not request.form.get("motivo", "").strip():
        flash("Escriba el motivo de la cancelación.", "error")
    elif nuevo in ("cancelada", "no_asistio") and sql("SELECT 1 FROM tratamientos WHERE cita_id = %s",
                                                      cita["id"]).fetchone():
        flash("En esa cita ya se anotaron tratamientos realizados: no se puede cancelar.", "error")
    else:
        motivo = request.form.get("motivo", "").strip()[:MOTIVO_MAXIMO] if nuevo == "cancelada" else ""
        indicaciones = request.form.get("indicaciones", "").strip()[:MOTIVO_MAXIMO] if nuevo == "atendida" else ""
        sql("UPDATE citas SET estado = %s, motivo_cancelacion = %s, indicaciones = %s WHERE id = %s",
            nuevo, motivo, indicaciones, cita["id"])
        anotar(f"Cita de {p['nombre']} del {cuando}: {ESTADOS_CITA[nuevo].lower()}" + (f" ({motivo})" if motivo else ""))
        flash(f"Cita {ESTADOS_CITA[nuevo].lower()}.", "ok")


@app.route("/salas", methods=["GET", "POST"])
def salas():
    """Plano de las clínicas, acomodadas como en las salas reales, con un check si están habilitadas o una X si no.
    El estudiante reporta las clínicas que no funcionan y la Facultad las marca como solucionadas."""
    yo, _, rol = quien()
    if request.method == "POST":
        accion = request.form.get("accion")
        r = s = None
        if rol == "estudiante" and accion == "reportar":
            s = sql("SELECT id, nombre FROM salas WHERE id = %s", request.form.get("sala", type=int)).fetchone()
            motivo, tipo = request.form.get("motivo", "").strip(), request.form.get("tipo", "equipo")
            if not s or not motivo or tipo not in TIPOS_FALLA:
                flash("Elija la clínica y escriba qué es lo que no funciona.", "error")
            elif len(motivo) > MOTIVO_MAXIMO:
                flash(f"El motivo puede tener hasta {MOTIVO_MAXIMO} caracteres.", "error")
            else:
                with bd().transaction():
                    nuevo = sql("INSERT INTO reportes (sala_id, tipo, descripcion, reportado_por_id) VALUES (%s, %s, %s, %s)"
                                " ON CONFLICT (sala_id) WHERE resuelto_en IS NULL DO NOTHING RETURNING id",
                                s["id"], tipo, motivo, yo).fetchone()
                    if nuevo:
                        sql("UPDATE salas SET habilitada = false WHERE id = %s", s["id"])
                        anotar(f"Reportó {s['nombre']} ({TIPOS_FALLA[tipo].lower()}): {motivo}")
                if nuevo:
                    flash(f"{s['nombre']} quedó como no funcional. La Facultad fue notificada.", "ok")
                else:
                    flash(f"{s['nombre']} ya tiene una falla reportada; la Facultad ya está avisada.", "error")
        elif rol == "facultad" and accion == "resolver":
            with bd().transaction():
                r = sql("UPDATE reportes SET resuelto_por_id = %s, resuelto_en = now()"
                        " WHERE id = %s AND resuelto_en IS NULL RETURNING sala_id, descripcion",
                        yo, request.form.get("reporte", type=int)).fetchone()
                if r:
                    s = sql("UPDATE salas SET habilitada = true WHERE id = %s RETURNING nombre", r["sala_id"]).fetchone()
                    anotar(f"Solucionó la falla de {s['nombre']}: {r['descripcion']}")
            if r:
                flash(f"{s['nombre']} quedó solucionada y habilitada otra vez.", "ok")
        elif rol == "facultad" and accion == "agregar":
            nombre, en_sala = request.form.get("nombre", "").strip(), request.form.get("en_sala", type=int)
            if not nombre or en_sala not in GRUPOS:
                flash("Escriba el nombre de la clínica y elija si es de pregrado o de posgrado.", "error")
            elif sql("SELECT 1 FROM salas WHERE lower(nombre) = lower(%s)", nombre).fetchone():
                flash("Ya existe una clínica con ese nombre.", "error")
            else:
                sql("INSERT INTO salas (nombre, en_sala) VALUES (%s, %s)", nombre, en_sala)
                anotar(f"Agregó {nombre} en {GRUPOS[en_sala]}")
                flash(f"{nombre} agregada.", "ok")
        elif rol == "facultad":
            s = sql("UPDATE salas SET habilitada = NOT habilitada WHERE id = %s RETURNING nombre, habilitada",
                    request.form.get("sala", type=int)).fetchone()
            if s:
                anotar(f"Marcó {s['nombre']} como " + ("habilitada" if s["habilitada"] else "no habilitada"))
        return redirect(url_for("salas"))
    historial = sql("SELECT s.nombre, r.tipo, r.descripcion AS motivo, r.reportado_por_id AS reporto,"
                    " r.resuelto_por_id AS resuelto_por,"
                    " to_char(r.creado_en AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS cuando,"
                    " to_char(r.resuelto_en AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS resuelto"
                    " FROM reportes r JOIN salas s ON s.id = r.sala_id WHERE r.resuelto_en IS NOT NULL"
                    " ORDER BY r.resuelto_en DESC LIMIT 100").fetchall() if rol != "estudiante" else []
    return render_template("salas.html", **comun(), salas=todas_las_salas(), nombre_de=nombre_de,
                           motivo_maximo=MOTIVO_MAXIMO, tipos_falla=TIPOS_FALLA, historial=historial)


@app.route("/pacientes/<int:pid>/odontograma", methods=["GET", "POST"])
def odontograma(pid):
    """Fotos del paciente y odontograma digital. El estudiante a cargo los llena y pide la revisión;
    el profesor lo aprueba o lo devuelve con lo que hay que corregir."""
    p = paciente(pid)
    if not p:
        return redirect(url_for("pacientes"))
    yo, _, rol = quien()
    if request.method == "POST":
        accion = request.form.get("accion")
        if rol == "estudiante" and accion in ("diente", "foto"):
            if accion == "diente":
                d, estado = request.form.get("diente", type=int), request.form.get("estado")
                nota = request.form.get("nota", "").strip()
                if d not in DIENTES or estado not in ESTADOS:
                    flash("Elija el diente y su estado.", "error")
                    return redirect(url_for("odontograma", pid=pid))
                que = f"el diente {d}: {ESTADOS[estado][0]}" + (f" ({nota})" if nota else "")
                guardar = ("INSERT INTO dientes (paciente_id, diente, estado, nota) VALUES (%s, %s, %s, %s)"
                           " ON CONFLICT (paciente_id, diente) DO UPDATE SET estado = excluded.estado,"
                           " nota = excluded.nota", pid, d, estado, nota)
            else:
                imagen = foto_jpeg(request.files.get("foto"))
                if not imagen:
                    flash("Elija una foto JPG o PNG de hasta 5 MB, o tómela con la cámara.", "error")
                    return redirect(url_for("odontograma", pid=pid))
                tipo = request.form.get("tipo", "intraoral")
                tipo = tipo if tipo in TIPOS_FOTO else "intraoral"
                que = f"una foto {TIPOS_FOTO[tipo].lower()}" if tipo != "radiografia" else "una radiografía"
                guardar = ("INSERT INTO fotos (paciente_id, tipo, url, descripcion) VALUES (%s, %s, %s, %s)", pid, tipo,
                           guardar_archivo("fotos", imagen), request.form.get("descripcion", "").strip() or None)
            with bd().transaction():
                sql(*guardar)
                # Si ya estaba aprobado y se cambia, el profesor lo tiene que volver a revisar
                sql("UPDATE pacientes SET odonto_revision = '' WHERE id = %s AND odonto_revision = 'aprobada'", pid)
                anotar(f"Anotó en el odontograma de {p['nombre']} {que}")
            flash("Foto guardada." if accion == "foto" else "Diente guardado.", "ok")
        elif rol == "estudiante" and accion == "pedir":
            if not sql("SELECT 1 FROM fotos WHERE paciente_id = %s AND tipo <> 'radiografia'", pid).fetchone():
                flash("Antes de pedir la revisión suba una foto del paciente (intraoral o extraoral).", "error")
            elif sql("UPDATE pacientes SET odonto_revision = 'pedida' WHERE id = %s"
                     " AND odonto_revision IN ('', 'devuelta') RETURNING id", pid).fetchone():
                anotar(f"Pidió la revisión del odontograma de {p['nombre']}")
                flash("Revisión pedida. El profesor la verá en su Inicio.", "ok")
        elif rol == "profesor" and accion in ("aprobar", "devolver"):
            nota = request.form.get("nota", "").strip()
            if accion == "devolver" and not nota:
                flash("Escriba qué tiene que corregir el estudiante.", "error")
            elif sql("UPDATE pacientes SET odonto_revision = %s, odonto_revisor_id = %s, odonto_nota = %s"
                     " WHERE id = %s AND odonto_revision = 'pedida' RETURNING id",
                     "aprobada" if accion == "aprobar" else "devuelta", yo, nota, pid).fetchone():
                anotar(("Aprobó el odontograma de " if accion == "aprobar" else "Devolvió el odontograma de ")
                       + p["nombre"] + (f": {nota}" if nota else ""))
                flash("Odontograma aprobado." if accion == "aprobar" else "Odontograma devuelto al estudiante.", "ok")
        return redirect(url_for("odontograma", pid=pid))
    elegido = request.args.get("diente", type=int)
    return render_template(
        "odontograma.html", **comun(), p=p, nombre_de=nombre_de, arriba=ARRIBA, abajo=ABAJO, estados=ESTADOS,
        revisiones=REVISIONES, tipos_foto=TIPOS_FOTO,
        elegido=elegido if rol == "estudiante" and not p["finalizado_en"] and elegido in DIENTES else None,
        dientes={f["diente"]: f for f in sql("SELECT * FROM dientes WHERE paciente_id = %s", pid)},
        fotos=sql("SELECT id, tipo, descripcion, to_char(creado_en AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI')"
                  " AS cuando FROM fotos WHERE paciente_id = %s ORDER BY id DESC", pid).fetchall())


@app.route("/pacientes/<int:pid>/fotos/<int:fid>.jpg")
def foto(pid, fid):
    # Solo la ve quien puede ver al paciente
    f = paciente(pid) and sql("SELECT url FROM fotos WHERE id = %s AND paciente_id = %s", fid, pid).fetchone()
    if not f:
        return redirect(url_for("pacientes"))
    return ver_archivo(f["url"])


def plan_de(pid):
    """El plan de tratamiento por fases. Un tratamiento esta realizado si tiene la cita en que se hizo: de ella
    salen quién lo hizo y cuándo."""
    return sql("SELECT t.*, a.nombre, a.codigo, c.estudiante_id AS realizado_por,"
               " to_char(c.inicio AT TIME ZONE 'America/Managua', 'YYYY-MM-DD') AS realizado"
               " FROM tratamientos t JOIN tarifario a ON a.id = t.tarifario_id LEFT JOIN citas c ON c.id = t.cita_id"
               " WHERE t.paciente_id = %s ORDER BY array_position(%s::text[], t.fase), t.id", pid, list(FASES)).fetchall()

def cuenta_de(pid):
    """Lo que cuesta el plan, lo realizado (lo que se cobra), lo pagado en recibos y el saldo pendiente."""
    c = sql("SELECT coalesce(sum(precio - descuento_monto), 0) AS plan,"
            " coalesce(sum(precio - descuento_monto) FILTER (WHERE cita_id IS NOT NULL), 0) AS realizado,"
            " (SELECT coalesce(sum(pagado), 0) FROM resumen_ordenes WHERE paciente_id = %s AND anulado_en IS NULL)"
            " AS abonado FROM tratamientos WHERE paciente_id = %s", pid, pid).fetchone()
    c["saldo"] = c["realizado"] - c["abonado"]
    return c


def registrar_pago(orden, p, m):
    """La Facultad registra un pago (recibo) de la orden; la base no deja pagar más que el saldo. Devuelve True si
    quedó guardado."""
    numero = request.form.get("numero", "").strip()
    metodo = request.form.get("metodo", "efectivo")
    referencia = request.form.get("referencia", "").strip() or None
    if m is None or m <= 0:
        flash("Escriba el monto del pago.", "error")
    elif m > orden["saldo"]:
        flash(f"El pago pasa el saldo de la orden ({cordobas(orden['saldo'])}).", "error")
    elif not numero or len(numero) > RECIBO_MAXIMO:
        flash(f"Escriba el número del recibo (hasta {RECIBO_MAXIMO} caracteres).", "error")
    elif metodo not in METODOS_PAGO or (metodo != "efectivo" and not referencia):
        flash("Elija cómo pagó y, si no fue en efectivo, el número de la transferencia o del voucher.", "error")
    elif sql("SELECT 1 FROM recibos WHERE numero = %s", numero).fetchone():
        flash("Ya hay un recibo con ese número.", "error")
    else:
        sql("INSERT INTO recibos (orden_id, numero, monto, metodo_pago, referencia, estudiante_id)"
            " VALUES (%s, %s, %s, %s, %s, %s)", orden["id"], numero, m, metodo, referencia, p["estudiante_id"])
        anotar(f"Registró el pago {numero} de {cordobas(m)} de la orden Nº {orden['id']} de {p['nombre']}")
        flash(f"Pago de {cordobas(m)} registrado.", "ok")
        return True
    return False


@app.route("/pacientes/<int:pid>/tratamientos", methods=["GET", "POST"])
def tratamientos(pid):
    """Plan de tratamiento con los precios del tarifario, las órdenes de cobro y los pagos del paciente.
    El estudiante a cargo anota y marca lo realizado (en qué cita); el profesor lo aprueba; el profesor o la
    Facultad generan la orden de cobro y la Facultad registra los pagos."""
    p = paciente(pid)
    if not p:
        return redirect(url_for("pacientes"))
    yo, _, rol = quien()
    if request.method == "POST":
        accion = request.form.get("accion")
        if rol == "estudiante" and accion == "anotar":
            proc = sql("SELECT * FROM tarifario WHERE id = %s AND activo",
                       request.form.get("procedimiento", type=int)).fetchone()
            diente = request.form.get("diente", type=int)  # opcional: hay procedimientos de toda la boca
            superficie = request.form.get("superficie", "") or None
            fase = request.form.get("fase", "basica")
            if not proc or (diente is not None and diente not in DIENTES) or fase not in FASES:
                flash("Elija el procedimiento del tarifario y la fase.", "error")
            elif superficie and (superficie not in SUPERFICIES or diente is None):
                flash("La cara del diente se elige junto con el diente.", "error")
            else:
                with bd().transaction():
                    if diente is not None:  # el tratamiento apunta al diente del odontograma: si no esta, esta sano
                        sql("INSERT INTO dientes (paciente_id, diente, estado) VALUES (%s, %s, 'sano')"
                            " ON CONFLICT DO NOTHING", pid, diente)
                    sql("INSERT INTO tratamientos (paciente_id, tarifario_id, diente, superficie, precio, fase)"
                        " VALUES (%s, %s, %s, %s, %s, %s)", pid, proc["id"], diente, superficie, proc["precio"], fase)
                    anotar(f"Anotó en el plan de {p['nombre']}: {proc['nombre']}" + (f" del {diente}" if diente else ""))
                flash("Tratamiento anotado en el plan.", "ok")
        elif rol == "estudiante" and accion in ("realizado", "quitar"):
            t = sql("SELECT t.id, t.orden_id, a.nombre FROM tratamientos t JOIN tarifario a ON a.id = t.tarifario_id"
                    " WHERE t.id = %s AND t.paciente_id = %s AND t.cita_id IS NULL",
                    request.form.get("id", type=int), pid).fetchone()
            cita = accion == "realizado" and sql(
                CITAS + " WHERE c.id = %s AND c.paciente_id = %s AND c.estado IN ('confirmada', 'atendida')"
                " AND c.inicio <= now()", request.form.get("cita", type=int), pid).fetchone()
            if t and accion == "realizado" and not p["firma"]:
                flash("Falta la firma del consentimiento informado: sin ella no se realiza ningún tratamiento.", "error")
            elif t and accion == "realizado" and not cita:
                flash("Elija la cita (confirmada o atendida, que ya pasó) en que se realizó.", "error")
            elif t and accion == "realizado":
                # la cita dice quién lo hizo y cuándo: a ese estudiante le toca subir el recibo de pago
                sql("UPDATE tratamientos SET cita_id = %s WHERE id = %s", cita["id"], t["id"])
                anotar(f"Realizó a {p['nombre']}: {t['nombre']} (cita del {dmy(cita['fecha'])})")
                flash(f"{t['nombre']} quedó como realizado.", "ok")
            elif t and t["orden_id"]:
                flash(f"{t['nombre']} ya está en la orden de cobro Nº {t['orden_id']}: no se quita.", "error")
            elif t:  # lo pendiente se puede quitar; lo realizado ya es historial
                sql("DELETE FROM tratamientos WHERE id = %s", t["id"])
                anotar(f"Quitó del plan de {p['nombre']}: {t['nombre']}")
                flash(f"{t['nombre']} quitado del plan.", "ok")
        elif rol == "profesor" and accion == "aprobar_tratamiento":
            t = sql("UPDATE tratamientos t SET aprobado_por_id = %s FROM tarifario a WHERE t.id = %s AND t.paciente_id = %s"
                    " AND t.aprobado_por_id IS NULL AND a.id = t.tarifario_id RETURNING a.nombre",
                    yo, request.form.get("id", type=int), pid).fetchone()
            if t:
                anotar(f"Aprobó el tratamiento {t['nombre']} de {p['nombre']}")
                flash(f"{t['nombre']} aprobado.", "ok")
        elif rol in ("profesor", "facultad") and accion == "orden":
            # RF16: la orden lleva todo lo del plan que todavia no esta en otra orden; el total lo calcula la base
            with bd().transaction():
                hay = sql("SELECT 1 FROM tratamientos WHERE paciente_id = %s AND orden_id IS NULL", pid).fetchone()
                orden = hay and sql("INSERT INTO ordenes (paciente_id, creado_por_id) VALUES (%s, %s) RETURNING id",
                                    pid, yo).fetchone()["id"]
                if orden:
                    sql("UPDATE tratamientos SET orden_id = %s WHERE paciente_id = %s AND orden_id IS NULL", orden, pid)
                    total = sql("SELECT total FROM resumen_ordenes WHERE id = %s", orden).fetchone()["total"]
                    anotar(f"Generó la orden de cobro Nº {orden} de {p['nombre']} por {cordobas(total)}")
            if not orden:
                flash("No hay tratamientos sin orden de cobro.", "error")
                return redirect(url_for("tratamientos", pid=pid))
            return redirect(url_for("orden", oid=orden))
        elif rol == "facultad" and accion == "pago":
            o = sql("SELECT * FROM resumen_ordenes WHERE id = %s AND paciente_id = %s AND anulado_en IS NULL",
                    request.form.get("orden", type=int), pid).fetchone()
            if not o:
                flash("Elija la orden de cobro que se paga.", "error")
            else:
                registrar_pago(o, p, monto(request.form.get("monto")))
        return redirect(url_for("tratamientos", pid=pid))
    ordenes = sql("SELECT *, to_char(creado_en AT TIME ZONE 'America/Managua', 'DD/MM/YYYY') AS creada"
                  " FROM resumen_ordenes WHERE paciente_id = %s ORDER BY id", pid).fetchall()
    return render_template(
        "tratamientos.html", **comun(), p=p, nombre_de=nombre_de, plan=plan_de(pid), cuenta=cuenta_de(pid),
        fases=FASES, superficies=SUPERFICIES, metodos=METODOS_PAGO, avance=avance_de(pid), ordenes=ordenes,
        por_pagar=[o for o in ordenes if o["estado"] in ("pendiente", "abonada")],
        sin_orden=sql("SELECT count(*) AS n FROM tratamientos WHERE paciente_id = %s AND orden_id IS NULL",
                      pid).fetchone()["n"],
        recibos=recibos_de(paciente_id=pid), dientes=ARRIBA + ABAJO,
        citas_hechas=sql(CITAS + " WHERE c.paciente_id = %s AND c.estado IN ('confirmada', 'atendida')"
                         " AND c.inicio <= now() ORDER BY c.inicio DESC", pid).fetchall(),
        tarifario=sql("SELECT * FROM tarifario WHERE activo ORDER BY nombre").fetchall())


@app.route("/ordenes/<int:oid>", methods=["GET", "POST"])
def orden(oid):
    """Orden de cobro para caja (RF16): numero, tratamientos con su precio y total. Se imprime o se muestra en
    pantalla; la Facultad registra los pagos (recibos) o la anula si se hizo por error."""
    o = sql("SELECT * FROM resumen_ordenes WHERE id = %s", oid).fetchone()
    p = o and paciente(o["paciente_id"])
    if not p:
        return redirect(url_for("pacientes"))
    _, _, rol = quien()
    if request.method == "POST" and rol == "facultad" and not o["anulado_en"]:
        if request.form.get("accion") == "anular":
            if o["pagado"] > 0:
                flash("La orden ya tiene pagos: anule primero sus recibos.", "error")
            else:
                with bd().transaction():  # sus tratamientos quedan libres para otra orden
                    sql("UPDATE tratamientos SET orden_id = NULL WHERE orden_id = %s", oid)
                    sql("UPDATE ordenes SET anulado_en = now() WHERE id = %s", oid)
                    anotar(f"Anuló la orden de cobro Nº {oid} de {p['nombre']}")
                flash(f"Orden Nº {oid} anulada; sus tratamientos pueden ir en otra orden.", "ok")
        else:  # cobrar: un pago por lo que falta, o por el monto que se escriba
            registrar_pago(o, p, monto(request.form.get("monto")) if request.form.get("monto") else o["saldo"])
        return redirect(url_for("orden", oid=oid))
    o["creada_texto"] = o["creado_en"].astimezone(MANAGUA).strftime("%d/%m/%Y %H:%M")
    return render_template("orden.html", **comun(), o=o, p=p, nombre_de=nombre_de, metodos=METODOS_PAGO,
                           recibos=sql("SELECT *, to_char(pagado_en AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI')"
                                       " AS cuando FROM recibos WHERE orden_id = %s ORDER BY id", oid).fetchall(),
                           creo=nombre_de(sql("SELECT creado_por_id FROM ordenes WHERE id = %s",
                                              oid).fetchone()["creado_por_id"]),
                           lineas=sql("SELECT t.*, a.nombre, a.codigo FROM tratamientos t JOIN tarifario a"
                                      " ON a.id = t.tarifario_id WHERE t.orden_id = %s ORDER BY t.id", oid).fetchall())


@app.route("/tarifario", methods=["GET", "POST"])
def tarifario():
    """Los procedimientos y su precio. Lo lleva la Facultad; un procedimiento desactivado ya no se anota.
    Cada cambio de precio guarda el anterior con el periodo en que estuvo vigente."""
    if quien()[2] != "facultad":
        return redirect(url_for("menu"))
    if request.method == "POST":
        accion, precio = request.form.get("accion"), monto(request.form.get("precio"))
        if accion == "agregar":
            nombre = request.form.get("nombre", "").strip()
            codigo = request.form.get("codigo", "").strip().upper()
            area = request.form.get("area", "")
            if not nombre or not codigo or precio is None or precio <= 0:
                flash("Escriba el código, el procedimiento y su precio, que tiene que ser mayor que cero.", "error")
            elif area not in ("", *AREAS):
                flash("Elija el área clínica de la lista.", "error")
            elif sql("SELECT 1 FROM tarifario WHERE lower(nombre) = lower(%s)", nombre).fetchone():
                flash("Ese procedimiento ya está en el tarifario.", "error")
            elif sql("SELECT 1 FROM tarifario WHERE codigo = %s", codigo).fetchone():
                flash("Ese código ya es de otro procedimiento.", "error")
            else:
                sql("INSERT INTO tarifario (codigo, nombre, precio, area) VALUES (%s, %s, %s, %s)",
                    codigo, nombre, precio, area)
                anotar(f"Agregó al tarifario {codigo} {nombre} a {cordobas(precio)}")
                flash(f"{nombre} agregado al tarifario.", "ok")
        elif accion == "precio":
            tid = request.form.get("id", type=int)
            antes = sql("SELECT precio FROM tarifario WHERE id = %s", tid).fetchone()
            fila = None
            if precio is not None and precio > 0 and antes and antes["precio"] != precio:
                with bd().transaction():  # el precio anterior queda en el historial, desde el cambio anterior
                    sql("INSERT INTO precios_anteriores (tarifario_id, precio, vigente_desde, vigente_hasta)"
                        " SELECT %s, %s, coalesce(max(vigente_hasta), '-infinity'), now() FROM precios_anteriores"
                        " WHERE tarifario_id = %s", tid, antes["precio"], tid)
                    fila = sql("UPDATE tarifario SET precio = %s WHERE id = %s RETURNING nombre", precio, tid).fetchone()
            if fila:
                anotar(f"Cambió el precio de {fila['nombre']} a {cordobas(precio)}")
                flash(f"Precio de {fila['nombre']} cambiado. Lo ya anotado conserva su precio.", "ok")
            else:
                flash("Escriba el precio nuevo, mayor que cero y distinto del actual.", "error")
        elif accion == "activo":
            fila = sql("UPDATE tarifario SET activo = NOT activo WHERE id = %s RETURNING nombre, activo",
                       request.form.get("id", type=int)).fetchone()
            if fila:
                anotar(("Activó" if fila["activo"] else "Desactivó") + f" en el tarifario {fila['nombre']}")
        return redirect(url_for("tarifario"))
    anteriores = {}
    for a in sql("SELECT tarifario_id, precio, CASE WHEN vigente_desde = '-infinity' THEN NULL ELSE"
                 " to_char(vigente_desde AT TIME ZONE 'America/Managua', 'DD/MM/YYYY') END AS desde,"
                 " to_char(vigente_hasta AT TIME ZONE 'America/Managua', 'DD/MM/YYYY') AS hasta"
                 " FROM precios_anteriores ORDER BY vigente_hasta DESC").fetchall():
        anteriores.setdefault(a["tarifario_id"], []).append(a)
    return render_template("tarifario.html", **comun(), areas=AREAS, anteriores=anteriores, nombre_de=nombre_de,
                           procedimientos=sql("SELECT * FROM tarifario ORDER BY area, nombre").fetchall())


ESTADO_SOLICITUD = {"pendiente": "Pendiente", "aprobada": "Entregada", "rechazada": "Rechazada"}
TIPOS_MOVIMIENTO = {"entrada": "Entrada", "salida": "Salida", "ajuste": "Ajuste"}

def existencias():
    """Los materiales con la cantidad en bodega, que la base calcula de los movimientos (vista existencias)."""
    return sql("SELECT e.*, m.activo, e.reponer AS bajo FROM existencias e JOIN materiales m ON m.id = e.id"
               " ORDER BY e.reponer DESC, e.nombre").fetchall()

def hay_de(material_id):
    fila = sql("SELECT cantidad FROM existencias WHERE id = %s", material_id).fetchone()
    return fila["cantidad"] if fila else None


@app.route("/inventario", methods=["GET", "POST"])
def inventario():
    """Materiales de la clínica. La Facultad anota lo que entra, lo que sale y los ajustes; la cantidad en bodega
    sale de esos movimientos, que no se editan ni se borran."""
    yo, _, rol = quien()
    if rol != "facultad":
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
                with bd().transaction():
                    mid = sql("INSERT INTO materiales (nombre, unidad, minimo) VALUES (%s, %s, %s) RETURNING id",
                              nombre, unidad or "unidad", minimo).fetchone()["id"]
                    if cantidad:  # lo que ya hay entra como el primer movimiento
                        sql("INSERT INTO movimientos_inventario (material_id, tipo, cantidad, responsable_id)"
                            " VALUES (%s, 'entrada', %s, %s)", mid, cantidad, yo)
                    anotar(f"Agregó al inventario {nombre}: {cantidad} {unidad}".rstrip())
                flash(f"{nombre} agregado al inventario.", "ok")
        elif accion in TIPOS_MOVIMIENTO:
            mid = request.form.get("id", type=int)
            hay = hay_de(mid)
            if hay is None or not cantidad or (accion != "ajuste" and cantidad < 1):
                flash("Escriba cuántas unidades entran o salen (en un ajuste, negativo si faltan).", "error")
            elif (accion == "salida" and cantidad > hay) or (accion == "ajuste" and hay + cantidad < 0):
                flash(f"No hay tantas unidades en el inventario (hay {hay}).", "error")
            else:
                m = sql("SELECT nombre, unidad FROM materiales WHERE id = %s", mid).fetchone()
                sql("INSERT INTO movimientos_inventario (material_id, tipo, cantidad, responsable_id)"
                    " VALUES (%s, %s, %s, %s)", mid, accion, cantidad, yo)
                quedan = hay_de(mid)
                anotar(f"{TIPOS_MOVIMIENTO[accion]} de {cantidad} {m['unidad']} de {m['nombre']}; quedan {quedan}")
                flash(f"{m['nombre']}: quedan {quedan} {m['unidad']}.", "ok")
        elif accion in ("entregar", "rechazar"):
            atender_solicitud(request.form.get("id", type=int), accion)
        return redirect(url_for("inventario"))
    return render_template("inventario.html", **comun(), nombre_de=nombre_de, solicitudes=solicitudes_de(),
                           materiales=existencias(), estado_solicitud=ESTADO_SOLICITUD, tipos=TIPOS_MOVIMIENTO,
                           movimientos=sql("SELECT v.*, m.nombre, m.unidad, to_char(v.creado_en AT TIME ZONE"
                                           " 'America/Managua', 'DD/MM/YYYY HH24:MI') AS cuando FROM movimientos_inventario v"
                                           " JOIN materiales m ON m.id = v.material_id ORDER BY v.id DESC LIMIT 50").fetchall())


def solicitudes_de(estudiante_id=None):
    return sql("SELECT s.*, m.nombre AS material, m.unidad, e.cantidad AS hay,"
               " to_char(s.creado_en AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS pedida FROM solicitudes s"
               " JOIN materiales m ON m.id = s.material_id JOIN existencias e ON e.id = s.material_id"
               " WHERE (%s::int IS NULL OR s.estudiante_id = %s)"
               " ORDER BY s.estado <> 'pendiente', s.id DESC LIMIT 200", estudiante_id, estudiante_id).fetchall()

def atender_solicitud(sid, accion):
    """La Facultad entrega lo pedido (se aprueba y sale de la bodega en un movimiento) o lo rechaza con una nota."""
    yo = quien()[0]
    s = sql("SELECT s.*, m.nombre, m.unidad FROM solicitudes s JOIN materiales m ON m.id = s.material_id"
            " WHERE s.id = %s AND s.estado = 'pendiente'", sid).fetchone()
    if not s:
        return
    if accion == "rechazar":
        nota = request.form.get("nota", "").strip()[:MOTIVO_MAXIMO]
        if not nota:
            flash("Escriba por qué se rechaza la solicitud.", "error")
            return
        sql("UPDATE solicitudes SET estado = 'rechazada', atendida_por_id = %s, nota = %s WHERE id = %s", yo, nota, sid)
        anotar(f"Rechazó la solicitud de {s['cantidad']} {s['unidad']} de {s['nombre']} de"
               f" {nombre_de(s['estudiante_id'])}: {nota}")
        flash("Solicitud rechazada.", "ok")
        return
    if s["cantidad"] > hay_de(s["material_id"]):
        flash(f"No hay suficiente {s['nombre']} para entregar lo pedido.", "error")
        return
    with bd().transaction():
        sql("UPDATE solicitudes SET estado = 'aprobada', atendida_por_id = %s WHERE id = %s", yo, sid)
        sql("INSERT INTO movimientos_inventario (material_id, solicitud_id, tipo, cantidad, responsable_id)"
            " VALUES (%s, %s, 'salida', %s, %s)", s["material_id"], sid, s["cantidad"], yo)
        quedan = hay_de(s["material_id"])
        anotar(f"Entregó {s['cantidad']} {s['unidad']} de {s['nombre']} a {nombre_de(s['estudiante_id'])};"
               f" quedan {quedan}")
    flash(f"Entregado. Quedan {quedan} {s['unidad']} de {s['nombre']}.", "ok")


@app.route("/materiales", methods=["GET", "POST"])
def materiales():
    """El estudiante ve que materiales hay y pide lo que necesita para su cita (RF17)."""
    yo, _, rol = quien()
    if rol != "estudiante":
        return redirect(url_for("inventario" if rol == "facultad" else "menu"))
    if request.method == "POST":
        m = sql("SELECT id, nombre, unidad FROM materiales WHERE id = %s AND activo",
                request.form.get("material", type=int)).fetchone()
        cantidad, para = request.form.get("cantidad", type=int), request.form.get("para", "").strip()
        try:
            para_ok = datetime.strptime(para, "%Y-%m-%d").date().isoformat() >= hoy()
        except ValueError:
            para_ok = False
        if not m or not cantidad or cantidad < 1:
            flash("Elija el material y cuántos necesita.", "error")
        elif not para_ok:
            flash("Elija en el calendario la fecha de la cita para la que lo necesita (hoy o después).", "error")
        else:
            sql("INSERT INTO solicitudes (material_id, cantidad, estudiante_id, para) VALUES (%s, %s, %s, %s)",
                m["id"], cantidad, yo, para)
            anotar(f"Pidió {cantidad} {m['unidad']} de {m['nombre']} para el {dmy(para)}")
            flash("Solicitud enviada a proveeduría.", "ok")
        return redirect(url_for("materiales"))
    return render_template("materiales.html", **comun(), hoy=hoy(), solicitudes=solicitudes_de(yo), nombre_de=nombre_de,
                           estado_solicitud=ESTADO_SOLICITUD,
                           materiales=[m for m in existencias() if m["activo"]])


def error_de_cuenta(f, anio, cid=None):
    """Por qué no se puede guardar la cuenta; None si se puede. El modelo pide correo único y, al estudiante, su
    año de la carrera."""
    if not f["nombre"] or len(f["nombre"]) > NOMBRE_MAXIMO or f["rol"] not in ROLES:
        return "Escriba el nombre y elija el rol."
    if not CORREO.fullmatch(f["correo"]) or len(f["correo"]) > 120:
        return "Escriba un correo válido."
    if sql("SELECT 1 FROM cuentas WHERE lower(correo) = lower(%s) AND id <> %s", f["correo"], cid or 0).fetchone():
        return "Ese correo ya es de otra cuenta."
    if f["rol"] == "estudiante" and anio not in ANIOS:
        return "Elija el año de la carrera del estudiante."
    return None


@app.route("/cuentas", methods=["GET", "POST"])
def administrar_cuentas():
    """La Facultad crea las cuentas, les pone contraseña y las desactiva. La contraseña se guarda cifrada."""
    yo, _, rol = quien()
    if rol != "facultad":
        return redirect(url_for("menu"))
    if request.method == "POST":
        accion, cif, clave = (request.form.get("accion"), request.form.get("cif", "").strip(),
                              request.form.get("clave", ""))
        c = por_cif(cif)
        if accion == "crear":
            f = {k: request.form.get(k, "").strip() for k in ("nombre", "rol", "correo")}
            anio = request.form.get("anio", type=int)
            if not cif.isalnum() or len(cif) > CIF_MAXIMO:
                flash("Escriba el CIF (solo números y letras).", "error")
            elif error := error_de_cuenta(f, anio):
                flash(error, "error")
            elif len(clave) < CLAVE_MINIMA:
                flash(f"La contraseña debe tener al menos {CLAVE_MINIMA} caracteres.", "error")
            elif c:
                flash("Ya existe una cuenta con ese CIF.", "error")
            else:
                sql("INSERT INTO cuentas (cif, nombre, correo, anio, rol, clave_hash) VALUES (%s, %s, %s, %s, %s, %s)",
                    cif, f["nombre"], f["correo"], anio if f["rol"] == "estudiante" else None, f["rol"], cifrar(clave))
                anotar(f"Creó la cuenta de {f['nombre']} ({cif}, {ROLES[f['rol']]})")
                flash(f"Cuenta de {f['nombre']} creada. Dele la contraseña en privado.", "ok")
        elif accion == "clave" and c:
            if len(clave) < CLAVE_MINIMA:
                flash(f"La contraseña debe tener al menos {CLAVE_MINIMA} caracteres.", "error")
            else:
                sql("UPDATE cuentas SET clave_hash = %s, fallos = 0, bloqueada_hasta = NULL WHERE id = %s",
                    cifrar(clave), c["id"])
                anotar(f"Le puso una contraseña nueva a {c['nombre']} ({cif})")
                flash(f"Contraseña de {c['nombre']} cambiada; si estaba bloqueada, ya puede entrar.", "ok")
        elif accion == "activa" and c:
            if c["id"] == yo:
                flash("No puede desactivar su propia cuenta.", "error")
            else:
                fila = sql("UPDATE cuentas SET activa = NOT activa WHERE id = %s RETURNING nombre, activa", c["id"]).fetchone()
                anotar(("Activó" if fila["activa"] else "Desactivó") + f" la cuenta de {fila['nombre']} ({cif})")
                flash(f"Cuenta de {fila['nombre']} " + ("activada." if fila["activa"] else "desactivada."), "ok")
        return redirect(url_for("administrar_cuentas", **request.args))
    filtro = {"rol": request.args.get("rol", ""), "estado": request.args.get("estado", "")}
    return render_template("cuentas.html", **comun(), roles=ROLES, clave_minima=CLAVE_MINIMA, filtro=filtro,
                           anios=ANIOS, nombre_de=nombre_de, correo_pendiente=CORREO_PENDIENTE, lista=sql(
        "SELECT c.id, c.cif, c.nombre, c.rol, c.activa, c.correo, c.anio, k.nombre AS clase,"
        " CASE WHEN c.bloqueada_hasta > now() THEN"
        " to_char(c.bloqueada_hasta AT TIME ZONE 'America/Managua', 'HH24:MI') END AS bloqueada"
        " FROM cuentas c LEFT JOIN clases k ON k.id = c.clase_id WHERE (%s = '' OR c.rol = %s)"
        " AND (%s = '' OR c.activa = (%s = 'activas')) ORDER BY c.activa DESC, c.rol, c.nombre",
        filtro["rol"], filtro["rol"], filtro["estado"], filtro["estado"]).fetchall())


CLASES = ("SELECT k.*, p.nombre AS profesor, (SELECT count(*) FROM cuentas e WHERE e.clase_id = k.id) AS estudiantes"
          " FROM clases k JOIN cuentas p ON p.id = k.profesor_id")


@app.route("/cuentas/<cif>", methods=["GET", "POST"])
def cuenta(cif):
    """Ficha de una cuenta para la Facultad: nombre, rol, correo y año (RF02); la clase del estudiante (RF06) y
    sus turnos clínicos (RF10)."""
    yo, _, rol = quien()
    if rol != "facultad":
        return redirect(url_for("menu"))
    c = sql("SELECT * FROM cuentas WHERE cif = %s", cif).fetchone()
    if not c:
        return redirect(url_for("administrar_cuentas"))
    clases = sql(CLASES + " ORDER BY k.periodo DESC, k.nombre").fetchall()
    if request.method == "POST":
        accion = request.form.get("accion")
        if accion == "datos":
            f = {k: request.form.get(k, "").strip() for k in ("nombre", "rol", "correo")}
            anio, clase = request.form.get("anio", type=int), request.form.get("clase", type=int)
            if error := error_de_cuenta(f, anio, c["id"]):
                flash(error, "error")
            elif c["id"] == yo and f["rol"] != rol:
                flash("No puede cambiar el rol de su propia cuenta.", "error")
            elif clase and clase not in {k["id"] for k in clases}:
                flash("Elija una clase de la lista.", "error")
            else:
                estudiante = f["rol"] == "estudiante"
                with bd().transaction():
                    sql("UPDATE cuentas SET nombre = %s, rol = %s, correo = %s, anio = %s, clase_id = %s WHERE id = %s",
                        f["nombre"], f["rol"], f["correo"], anio if estudiante else None,
                        clase if estudiante else None, c["id"])
                    if not estudiante:
                        sql("DELETE FROM turnos WHERE estudiante_id = %s", c["id"])
                    nueva = next((k for k in clases if k["id"] == clase), None)
                    anotar(f"Corrigió la cuenta {cif}: {f['nombre']}, {ROLES[f['rol']]}"
                           + (f", clase {nueva['nombre']} ({nueva['profesor']})" if estudiante and nueva else ""))
                flash(f"Cuenta de {f['nombre']} guardada.", "ok")
        elif accion == "turno" and c["rol"] == "estudiante":
            dia = request.form.get("dia", type=int)
            desde, hasta = request.form.get("desde", ""), request.form.get("hasta", "")
            horas = all(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", h) for h in (desde, hasta))
            if dia not in DIAS or not horas or desde >= hasta:
                flash("Elija el día y las horas del turno; la hora de inicio va antes de la de fin.", "error")
            elif desde < APERTURA or hasta > CIERRE:
                flash(f"El turno tiene que estar dentro del horario de la clínica, de {APERTURA} a {CIERRE}.", "error")
            elif sql("SELECT 1 FROM turnos WHERE estudiante_id = %s AND dia = %s AND desde < %s AND %s < hasta",
                     c["id"], dia, hasta, desde).fetchone():
                flash("Ese turno se cruza con otro que ya tiene ese día.", "error")
            else:
                sql("INSERT INTO turnos (estudiante_id, dia, desde, hasta) VALUES (%s, %s, %s, %s)", c["id"], dia, desde, hasta)
                anotar(f"Asignó a {c['nombre']} el turno del {DIAS[dia].lower()} de {desde} a {hasta}")
                flash("Turno agregado.", "ok")
        elif accion == "quitar_turno":
            t = sql("DELETE FROM turnos WHERE id = %s AND estudiante_id = %s RETURNING dia, desde, hasta",
                    request.form.get("id", type=int), c["id"]).fetchone()
            if t:
                anotar(f"Quitó a {c['nombre']} el turno del {DIAS[t['dia']].lower()} de {t['desde']:%H:%M} a {t['hasta']:%H:%M}")
                flash("Turno quitado.", "ok")
        return redirect(url_for("cuenta", cif=cif))
    return render_template("cuenta.html", **comun(), c=c, roles=ROLES, anios=ANIOS, dias=DIAS, clases=clases,
                           turnos=turnos_de(c["id"]), nombre_maximo=NOMBRE_MAXIMO, apertura=APERTURA, cierre=CIERRE,
                           correo_pendiente=CORREO_PENDIENTE,
                           sus_clases=[k for k in clases if k["profesor_id"] == c["id"]])


def qr(direccion):
    """El código QR de una dirección, como SVG listo para el src de un <img>."""
    dibujo = Drawing(120, 120)
    dibujo.add(QrCodeWidget(direccion, barWidth=120, barHeight=120, barLevel="M"))
    return "data:image/svg+xml;base64," + base64.b64encode(renderSVG.drawToString(dibujo).encode()).decode()


@app.route("/clases", methods=["GET", "POST"])
def clases():
    """Las clases. El profesor crea las suyas (nombre y periodo); la Facultad las ve todas."""
    yo, _, rol = quien()
    if rol == "estudiante":
        return redirect(url_for("menu"))
    if request.method == "POST" and rol == "profesor":
        nombre, periodo = request.form.get("nombre", "").strip(), request.form.get("periodo", "").strip().upper()
        if not nombre or len(nombre) > NOMBRE_MAXIMO or not PERIODO.fullmatch(periodo):
            flash("Escriba el nombre de la clase y su periodo, por ejemplo 2026-II.", "error")
        elif sql("SELECT 1 FROM clases WHERE nombre = %s AND periodo = %s AND profesor_id = %s",
                 nombre, periodo, yo).fetchone():
            flash("Ya tiene una clase con ese nombre en ese periodo.", "error")
        else:
            kid = sql("INSERT INTO clases (nombre, periodo, profesor_id) VALUES (%s, %s, %s) RETURNING id",
                      nombre, periodo, yo).fetchone()["id"]
            anotar(f"Creó la clase {nombre} ({periodo})")
            return redirect(url_for("clase", kid=kid))
        return redirect(url_for("clases"))
    ahora = datetime.now(MANAGUA)
    periodo = f"{ahora.year}-{'I' if ahora.month <= 6 else 'II'}"
    return render_template("clases.html", **comun(), periodo=periodo, nombre_maximo=NOMBRE_MAXIMO, clases=sql(
        CLASES + " WHERE %s OR k.profesor_id = %s ORDER BY k.periodo DESC, k.nombre", rol == "facultad", yo).fetchall())


# Cada codigo QR de una clase con su estado: se usa una sola vez y vence
INVITACIONES = ("SELECT i.*, to_char(i.vence_en AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS hasta,"
                " CASE WHEN i.usada_en IS NOT NULL THEN 'Usado' WHEN i.vence_en <= now() THEN 'Vencido'"
                " ELSE 'Disponible' END AS estado FROM invitaciones i")


@app.route("/clases/<int:kid>", methods=["GET", "POST"])
def clase(kid):
    """Una clase: sus estudiantes y sus códigos QR. El profesor genera códigos (cada uno sirve una sola vez) y
    los imprime o proyecta para que sus estudiantes entren a la clase."""
    yo, _, rol = quien()
    k = rol != "estudiante" and sql(CLASES + " WHERE k.id = %s AND (%s OR k.profesor_id = %s)",
                                    kid, rol == "facultad", yo).fetchone()
    if not k:
        return redirect(url_for("menu"))
    if request.method == "POST" and k["profesor_id"] == yo:
        cuantos, dias = request.form.get("cantidad", type=int), request.form.get("dias", type=int)
        if not cuantos or not dias or not 1 <= cuantos <= CODIGOS_MAXIMO or not 1 <= dias <= DIAS_MAXIMO:
            flash(f"Escriba cuántos códigos (hasta {CODIGOS_MAXIMO}) y cuántos días sirven (hasta {DIAS_MAXIMO}).",
                  "error")
        else:
            with bd().transaction():
                for _ in range(cuantos):
                    sql("INSERT INTO invitaciones (codigo, clase_id, vence_en) VALUES (%s, %s, now() + %s * interval '1 day')",
                        secrets.token_urlsafe(9), kid, dias)
                anotar(f"Generó {cuantos} códigos QR de la clase {k['nombre']} ({k['periodo']}), por {dias} días")
            flash(f"{cuantos} códigos generados. Cada uno sirve para un estudiante.", "ok")
        return redirect(url_for("clase", kid=kid))
    codigos = sql(INVITACIONES + " WHERE i.clase_id = %s ORDER BY i.usada_en NULLS FIRST, i.id", kid).fetchall()
    for c in codigos:
        if c["estado"] == "Disponible":
            c["direccion"] = SITIO + url_for("registro", codigo=c["codigo"])
            c["qr"] = qr(c["direccion"])
    return render_template("clase.html", **comun(), k=k, codigos=codigos, codigos_maximo=CODIGOS_MAXIMO,
                           dias_maximo=DIAS_MAXIMO, es_suya=k["profesor_id"] == yo, estudiantes=sql(
                               "SELECT cif, nombre, correo, anio, activa FROM cuentas WHERE clase_id = %s ORDER BY nombre",
                               kid).fetchall())


def usar_codigo(codigo, cuenta_id):
    """Pone al estudiante en la clase del código (la base lo hace en una sola operación y lo anota en la bitácora).
    Devuelve el error para mostrar, o None si entró."""
    try:
        with bd().transaction():
            sql("SELECT usar_invitacion(%s, %s)", codigo, cuenta_id)
    except psycopg.errors.RaiseException as e:
        return str(e.diag.message_primary or e)
    return None


@app.route("/registro/<codigo>", methods=["GET", "POST"])
def registro(codigo):
    """Página a la que lleva el QR de una clase. Un estudiante nuevo crea su cuenta (CIF, nombre, correo, año y
    contraseña) y entra a la clase; uno que ya tiene cuenta entra con su contraseña y pasa a esta clase.
    Cada código sirve una sola vez y vence; solo crea cuentas de estudiante."""
    invitacion = sql(INVITACIONES + " WHERE i.codigo = %s", codigo).fetchone()
    k = invitacion and sql(CLASES + " WHERE k.id = %s", invitacion["clase_id"]).fetchone()
    datos = {"cif": "", "nombre": "", "correo": "", "anio": None, "errores": {}}
    yo = quien()
    if request.method == "POST" and invitacion and invitacion["estado"] == "Disponible":
        if request.form.get("accion") == "entrar":  # ya tiene cuenta: inicia sesion y vuelve aqui
            session["codigo_clase"] = codigo
            return redirect(url_for("entrar"))
        if request.form.get("accion") == "unirse" and yo and yo[2] == "estudiante":
            if error := usar_codigo(codigo, yo[0]):
                flash(error, "error")
                return redirect(url_for("registro", codigo=codigo))
            flash(f"Ya está en la clase {k['nombre']} de {k['profesor']}.", "ok")
            return redirect(url_for("menu"))
        cif, nombre, correo = (request.form.get(c, "").strip() for c in ("cif", "nombre", "correo"))
        anio = request.form.get("anio", type=int)
        clave, errores = request.form.get("clave", ""), datos["errores"]
        datos.update(cif=cif, nombre=nombre, correo=correo, anio=anio)
        ya_existe = "Ya existe una cuenta con ese CIF. Si es suya, entre con su contraseña; si no, avise a la Facultad."
        if not cif.isalnum() or len(cif) > CIF_MAXIMO:
            errores["cif"] = "Escriba su CIF, solo con números y letras."
        elif por_cif(cif):
            errores["cif"] = ya_existe
        if not nombre or len(nombre) > NOMBRE_MAXIMO:
            errores["nombre"] = "Escriba su nombre completo."
        if not CORREO.fullmatch(correo) or len(correo) > 120:
            errores["correo"] = "Escriba su correo, por ejemplo nombre@uamv.edu.ni."
        elif sql("SELECT 1 FROM cuentas WHERE lower(correo) = lower(%s)", correo).fetchone():
            errores["correo"] = "Ese correo ya es de otra cuenta."
        if anio not in ANIOS:
            errores["anio"] = "Elija el año de la carrera que cursa."
        if len(clave) < CLAVE_MINIMA:
            errores["clave"] = f"La contraseña debe tener al menos {CLAVE_MINIMA} caracteres."
        elif clave != request.form.get("clave2", ""):
            errores["clave2"] = "Las dos contraseñas no son iguales."
        if not errores:
            cifrada = cifrar(clave)
            try:
                with bd().transaction():  # la cuenta y su entrada a la clase, juntas o ninguna
                    cid = sql("INSERT INTO cuentas (cif, nombre, correo, anio, rol, clave_hash)"
                              " VALUES (%s, %s, %s, %s, 'estudiante', %s) RETURNING id",
                              cif, nombre, correo, anio, cifrada).fetchone()["id"]
                    sql("SELECT usar_invitacion(%s, %s)", codigo, cid)
            except psycopg.errors.UniqueViolation:  # otro la creó con ese CIF o correo en el mismo instante
                errores["cif"] = ya_existe
            except psycopg.errors.RaiseException as e:  # el código se usó o venció mientras llenaba el formulario
                flash(str(e.diag.message_primary or e), "error")
            else:
                g.pop("cuentas", None)  # para que la sesión ya encuentre la cuenta nueva
                session.clear()
                session.permanent = True
                session["cuenta"] = cid
                anotar(f"Creó su cuenta con un código de la clase {k['nombre']}")
                flash("Cuenta creada. La próxima vez entre con su CIF y la contraseña que acaba de crear.", "ok")
                return redirect(url_for("menu"))
    return render_template("registro.html", **datos, invitacion=invitacion, k=k, yo=yo, clave_minima=CLAVE_MINIMA,
                           anios=ANIOS, cif_maximo=CIF_MAXIMO, nombre_maximo=NOMBRE_MAXIMO)


def recibos_de(estudiante_id=None, paciente_id=None):
    """Los recibos (pagos de las órdenes de cobro) que van en la hoja de control, con su paciente y lo que se
    pagó; de un estudiante o de un paciente. Los anulados no cuentan."""
    return sql("SELECT r.*, to_char(r.pagado_en AT TIME ZONE 'America/Managua', 'YYYY-MM-DD') AS fecha,"
               " p.id AS pid, p.nombre AS paciente, p.expediente,"
               " (SELECT string_agg(a.nombre || coalesce(' del ' || t.diente, ''), ', ' ORDER BY t.id)"
               " FROM tratamientos t JOIN tarifario a ON a.id = t.tarifario_id WHERE t.orden_id = r.orden_id) AS tratamiento"
               " FROM recibos r JOIN ordenes o ON o.id = r.orden_id JOIN pacientes p ON p.id = o.paciente_id"
               " WHERE r.anulado_en IS NULL AND p.eliminado_en IS NULL AND (%s::int IS NULL OR r.estudiante_id = %s)"
               " AND (%s::int IS NULL OR p.id = %s) ORDER BY r.pagado_en, r.id",
               estudiante_id, estudiante_id, paciente_id, paciente_id).fetchall()


@app.route("/control", methods=["GET", "POST"])
def control():
    """Hoja de control de expediente y recibos de pago. El estudiante sube cada recibo con su número, monto y foto
    (o le pone la foto a uno que registró la Facultad); el profesor firma cada uno; al final del curso se imprime."""
    yo, _, rol = quien()
    if request.method == "POST":
        accion = request.form.get("accion")
        if rol == "estudiante" and accion == "subir":
            # Solo de una orden de un paciente suyo que todavía tiene saldo
            o = sql("SELECT r.* FROM resumen_ordenes r JOIN pacientes p ON p.id = r.paciente_id WHERE r.id = %s"
                    " AND p.estudiante_id = %s AND r.anulado_en IS NULL",
                    request.form.get("orden", type=int), yo).fetchone()
            imagen = foto_jpeg(request.files.get("foto"))
            if not o:
                flash("Elija la orden de cobro que se pagó.", "error")
            elif not imagen:
                flash("Suba la foto del recibo: JPG o PNG de hasta 5 MB, o tómela con la cámara.", "error")
            else:
                p = sql("SELECT * FROM pacientes WHERE id = %s", o["paciente_id"]).fetchone()
                url = guardar_archivo("recibos", imagen)
                if registrar_pago(o, p, monto(request.form.get("monto"))):
                    sql("UPDATE recibos SET url = %s, estudiante_id = %s WHERE numero = %s",
                        url, yo, request.form.get("numero", "").strip())
                else:
                    borrar_archivos([url])
        elif rol == "estudiante" and accion == "foto":
            imagen = foto_jpeg(request.files.get("foto"))
            r = imagen and sql("UPDATE recibos SET url = %s WHERE id = %s AND estudiante_id = %s AND url IS NULL"
                               " RETURNING numero", guardar_archivo("recibos", imagen),
                               request.form.get("id", type=int), yo).fetchone()
            if r:
                anotar(f"Subió la foto del recibo {r['numero']}")
                flash(f"Foto del recibo {r['numero']} guardada.", "ok")
            else:
                flash("Elija la foto del recibo: JPG o PNG de hasta 5 MB.", "error")
        elif rol == "estudiante" and accion == "quitar":
            # Un recibo no se borra: se anula. Lo firmado por el profesor ya no se anula
            r = sql("UPDATE recibos SET anulado_en = now() WHERE id = %s AND estudiante_id = %s AND firmado_por_id IS NULL"
                    " AND anulado_en IS NULL RETURNING numero", request.form.get("id", type=int), yo).fetchone()
            if r:
                anotar(f"Anuló el recibo {r['numero']} de su hoja de control")
                flash(f"Recibo {r['numero']} anulado.", "ok")
        elif rol == "profesor" and accion in ("firmar", "quitar_firma"):
            if accion == "firmar":  # solo los recibos de los estudiantes de sus clases
                r = sql("UPDATE recibos SET firmado_por_id = %s WHERE id = %s AND firmado_por_id IS NULL"
                        " AND es_profesor_de(%s, estudiante_id) RETURNING numero, estudiante_id",
                        yo, request.form.get("id", type=int), yo).fetchone()
            else:  # solo el profesor que firmó puede quitar su firma
                r = sql("UPDATE recibos SET firmado_por_id = NULL WHERE id = %s AND firmado_por_id = %s"
                        " RETURNING numero, estudiante_id", request.form.get("id", type=int), yo).fetchone()
            if r:
                anotar(("Firmó" if accion == "firmar" else "Quitó su firma de")
                       + f" el recibo {r['numero']} de {nombre_de(r['estudiante_id'])}")
                flash(f"Recibo {r['numero']} " + ("firmado." if accion == "firmar" else "sin firma otra vez."), "ok")
        return redirect(url_for("control", estudiante=request.args.get("estudiante") or None))
    estudiantes = [(e["id"], e["nombre"]) for e in sql(
        "SELECT id, nombre FROM cuentas WHERE rol = 'estudiante' AND (%s OR es_profesor_de(%s, id)) ORDER BY nombre",
        rol == "facultad", yo).fetchall()]
    elegido = yo if rol == "estudiante" else request.args.get("estudiante", type=int)
    if rol != "estudiante" and elegido not in dict(estudiantes):
        elegido = None
    ordenes = []  # las órdenes de sus pacientes con saldo, para elegir a cuál corresponde el recibo
    if rol == "estudiante":
        ordenes = sql("SELECT r.*, p.nombre AS paciente FROM resumen_ordenes r JOIN pacientes p ON p.id = r.paciente_id"
                      " WHERE p.estudiante_id = %s AND r.anulado_en IS NULL AND r.saldo > 0 AND p.eliminado_en IS NULL"
                      " ORDER BY p.nombre, r.id", yo).fetchall()
    recibos = recibos_de(estudiante_id=elegido)
    if rol == "profesor":  # sin elegir estudiante, los recibos de todos los de sus clases
        recibos = [r for r in recibos if r["estudiante_id"] in dict(estudiantes)]
    return render_template("control.html", **comun(), recibos=recibos, elegido=elegido, metodos=METODOS_PAGO,
                           estudiantes=estudiantes, ordenes=ordenes, hoy=hoy(), nombre_de=nombre_de,
                           recibo_maximo=RECIBO_MAXIMO)


@app.route("/control/recibos/<int:rid>.jpg")
def recibo_foto(rid):
    # El estudiante ve solo sus recibos; el profesor, los de sus clases; la Facultad, todos
    yo, _, rol = quien()
    r = sql("SELECT url FROM recibos WHERE id = %s AND url IS NOT NULL AND CASE %s WHEN 'estudiante' THEN"
            " estudiante_id = %s WHEN 'profesor' THEN es_profesor_de(%s, estudiante_id) ELSE true END",
            rid, rol, yo, yo).fetchone()
    if not r:
        return redirect(url_for("control"))
    return ver_archivo(r["url"])


@app.route("/comentarios")
def comentarios():
    """Lleva al formulario de Google donde estudiantes y profesores dejan sus observaciones del sistema."""
    if not COMENTARIOS or ("Comentarios", "comentarios") not in MENUS[quien()[2]]:
        return redirect(url_for("menu"))
    return render_template("comentarios.html", **comun(), formulario=COMENTARIOS)


@app.route("/bitacora")
def bitacora():
    """Quién hizo qué y a qué hora. La revisan el profesor y la Facultad."""
    if quien()[2] == "estudiante":
        return redirect(url_for("menu"))
    # RF18: se filtra por fechas, por usuario (nombre o CIF) y por lo que se hizo. Se muestran hasta 500 lineas
    f = {c: request.args.get(c, "").strip() for c in ("desde", "hasta", "usuario", "accion")}
    for c in ("desde", "hasta"):
        try:
            datetime.strptime(f[c], "%Y-%m-%d")
        except ValueError:
            f[c] = ""
    como = lambda t: "%" + re.sub(r"([\\%_])", r"\\\1", t) + "%"  # noqa: E731
    lineas = sql("SELECT to_char(b.creado_en AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS cuando,"
                 " b.cif_registrado AS cif, coalesce(c.nombre, 'Sistema') AS nombre, c.rol, b.accion, b.lugar"
                 " FROM bitacora b LEFT JOIN cuentas c ON c.id = b.cuenta_id"
                 " WHERE (%s = '' OR (b.creado_en AT TIME ZONE 'America/Managua')::date >= nullif(%s, '')::date)"
                 " AND (%s = '' OR (b.creado_en AT TIME ZONE 'America/Managua')::date <= nullif(%s, '')::date)"
                 " AND (%s = '' OR coalesce(c.nombre, 'Sistema') ILIKE %s OR b.cif_registrado ILIKE %s)"
                 " AND (%s = '' OR b.accion ILIKE %s) ORDER BY b.id DESC LIMIT 500", f["desde"], f["desde"],
                 f["hasta"], f["hasta"], f["usuario"], como(f["usuario"]), como(f["usuario"]), f["accion"],
                 como(f["accion"])).fetchall()
    return render_template("bitacora.html", **comun(), lineas=lineas, f=f, filtrado=any(f.values()), roles=ROLES)


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
    p["trabajo"] = trabajo_de(p)
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
                            ("Estudiante a cargo", nombre_de(p["estudiante_id"])),
                            ("Estado", "Aprobado" if p["aprobado"] else f"Rechazado: {p['rechazo']}" if p["rechazo"]
                             else "Pendiente de aprobación")):
        linea(f"{etiqueta}: {valor}")
    if p["finalizado_en"]:
        linea(f"Recibió el alta el {p['finalizado_en'].astimezone(MANAGUA):%d/%m/%Y} por {nombre_de(p['finalizado_por_id'])}: "
              f"{p['finalizado_resumen']}")

    y -= 10
    h = historia_de(pid)
    linea("Historia clínica" + ("" if h.get("completa") else " (BORRADOR, sin completar)"), "Helvetica-Bold", 12)
    for c, etiqueta in {**DEL_PACIENTE, **HISTORIA}.items():
        if c == "sexo" and h.get(c):
            linea(f"{etiqueta}: {SEXOS.get(h[c], h[c])}", "Helvetica", 10)
        elif h.get(c):
            linea(f"{etiqueta}: {dmy(h[c]) if c == 'nacimiento' else h[c]}"[:110], "Helvetica", 10)
    for a in alertas_de(h):
        linea(f"ALERTA - {a}"[:110], "Helvetica-Bold", 10)

    y -= 10
    linea("Trabajo por estudiante", "Helvetica-Bold", 12)
    for t in p["trabajo"]:
        hasta = t["hasta"].astimezone(MANAGUA).strftime(FORMATO) if t["hasta"] else "hoy"
        linea(f"- {nombre_de(t['estudiante_id'])}: {t['desde'].astimezone(MANAGUA):%d/%m/%Y} a {hasta}."
              f" {t['resumen'] or ''}", "Helvetica", 10)

    agenda = sql(CITAS + " WHERE c.paciente_id = %s ORDER BY c.inicio", pid).fetchall()
    if agenda:
        y -= 10
        linea("Citas", "Helvetica-Bold", 12)
        for c in agenda:
            linea(f"- {dmy(c['fecha'])} {c['hora']}-{c['hora_fin']} · {c['razon']} · {c['sala']} · {ESTADOS_CITA[c['estado']]}"
                  + (f" ({c['motivo_cancelacion']})" if c["motivo_cancelacion"] else ""), "Helvetica", 10)

    y -= 10
    linea("Odontograma", "Helvetica-Bold", 12)
    linea(f"Revisión del profesor: {REVISIONES[p['odonto_revision']]}"
          + (f" ({nombre_de(p['odonto_revisor_id'])})" if p["odonto_revision"] in ("aprobada", "devuelta") else ""),
          "Helvetica", 10)
    hallazgos = sql("SELECT * FROM dientes WHERE paciente_id = %s AND estado <> 'sano' ORDER BY diente", pid).fetchall()
    for d in hallazgos:
        linea(f"- Diente {d['diente']}: {ESTADOS[d['estado']][0]}" + (f" · {d['nota']}" if d["nota"] else ""), "Helvetica", 10)
    if not hallazgos:
        linea("Sin hallazgos anotados: todos los dientes sanos.", "Helvetica", 10)

    y -= 10
    linea("Tratamientos y cobros", "Helvetica-Bold", 12)
    for t in plan_de(pid):
        linea(f"- {FASES[t['fase']]}: {t['nombre']}" + (f" del {t['diente']}" if t["diente"] else "") + f" · {cordobas(t['precio'] - t['descuento_monto'])} · "
              + (f"realizado el {dmy(t['realizado'])}" if t["realizado"] else "pendiente"), "Helvetica", 10)
    c = cuenta_de(pid)
    linea(f"Realizado: {cordobas(c['realizado'])} · Pagado: {cordobas(c['abonado'])} · "
          f"Saldo pendiente: {cordobas(c['saldo'])}", "Helvetica", 10)
    for r in recibos_de(paciente_id=pid):
        linea(f"- Recibo {r['numero']} · {dmy(r['fecha'])} · {cordobas(r['monto'])} · "
              + (f"firmado por {nombre_de(r['firmado_por_id'])}" if r["firmado_por_id"] else "sin firma del docente"),
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
        linea(f"Firma del paciente · {p['firmado_en'].astimezone(MANAGUA):%d/%m/%Y}", "Helvetica", 9)

    hoja.showPage()
    hoja.save()
    return Response(archivo.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename=expediente_{p['cedula']}.pdf"})


@app.route("/privacidad")
def privacidad():
    """Politica de privacidad publica (Ley 787). Meta la pide para publicar la app de WhatsApp."""
    return render_template("privacidad.html")


@app.route("/salir")
def salir():
    session.clear()
    return redirect(url_for("entrar"))


if __name__ == "__main__":
    crear_tablas()
    print("\n  Clínicas Odontológicas:  http://localhost:5001\n")
    app.run(port=5001)
