"""Clínicas Odontológicas (Flask + HTML), con los datos en Neon (PostgreSQL en la nube).

Necesita:  pip install -r requirements.txt
Variables: DATABASE_URL (cadena de conexión de Neon), SECRET_KEY, el almacenamiento de las fotos (ALMACEN_*, ver
           abajo) y, opcionales, CLAVE_INICIAL, SITIO y COMENTARIOS
Ejecutar:  python prototipo.py   (crea las tablas si no existen)
"""
import base64
import io
import ipaddress
import json
import os
import re
import secrets
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import boto3
import psycopg
from botocore.config import Config
from flask import (Flask, Response, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
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
# Enlace del formulario de Google donde estudiantes y profesores dejan sus observaciones para mejorar el
# sistema. Si se deja vacío (o no es una dirección https), la pestaña Comentarios no aparece
COMENTARIOS = os.environ.get("COMENTARIOS", "https://docs.google.com/forms/d/e/1FAIpQLSe1BhT__8TPsUv_Iowl4nalISPvR-"
                                            "0GqE3LUA0LYqvooZVDjw/viewform").strip()
COMENTARIOS = COMENTARIOS if COMENTARIOS.startswith("https://") else ""
ROLES = {"profesor": "Profesor", "estudiante": "Estudiante", "admin": "Facultad"}
# Pestanas de cada rol: (nombre, endpoint). La bitacora la revisan el profesor y la Facultad
MENUS = {"estudiante": [("Inicio", "menu"), ("Mis pacientes", "pacientes"), ("Mis citas", "citas"), ("Clínicas", "salas"),
                        ("Materiales", "materiales"), ("Hoja de control", "control"), ("Comentarios", "comentarios")],
         # El profesor solo revisa: no registra pacientes, citas ni tratamientos
         "profesor": [("Inicio", "menu"), ("Pacientes", "pacientes"), ("Citas", "citas"), ("Clínicas", "salas"),
                      ("Hoja de control", "control"), ("Bitácora", "bitacora"), ("Comentarios", "comentarios")],
         "admin": [("Inicio", "menu"), ("Pacientes", "pacientes"), ("Citas", "citas"), ("Clínicas", "salas"),
                   ("Tarifario", "tarifario"), ("Inventario", "inventario"), ("Cuentas", "administrar_cuentas"),
                   ("Hoja de control", "control"), ("Bitácora", "bitacora")]}
QUE_HACE = {"estudiante": ["Registrar a sus pacientes y la razón por la que vienen",
                           "Agendar sus citas con la razón de cada cita, confirmarlas, reprogramarlas o cancelarlas",
                           "Marcar si el paciente llegó a su cita o no se presentó",
                           "Subir la foto presencial, llenar el odontograma y pedir la revisión del profesor",
                           "Llenar la historia clínica y anotar el plan de tratamiento por fases",
                           "Pedir materiales a proveeduría y generar la orden de cobro para caja",
                           "Recoger la firma del paciente en el consentimiento informado e imprimirlo",
                           "Subir la foto de cada recibo de pago y entregar impresa la hoja de control"],
            "profesor": ["Aprobar a los pacientes que registra cada estudiante o rechazarlos con una observación",
                         "Aprobar el odontograma o devolverlo con lo que hay que corregir",
                         "Ver el historial de qué le hizo cada estudiante a cada paciente",
                         "Firmar los recibos de pago de la hoja de control de cada estudiante",
                         "Revisar la bitácora del sistema",
                         "No registra pacientes, citas ni tratamientos"],
            "admin": ["Crear, corregir y desactivar las cuentas",
                      "Entregar los materiales que piden los estudiantes y cobrar las órdenes de cobro",
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
# Ciclo de vida de una cita (RF11): de cada estado activo, a cuales puede pasar. Las demas ya no cambian
ACTIVAS = ("Programada", "Confirmada")
SIGUIENTE = {"Programada": ("Confirmada", "Atendida", "No se presentó", "Cancelada"),
             "Confirmada": ("Atendida", "No se presentó", "Cancelada")}
COLOR_CITA = {"Programada": "", "Confirmada": "verde", "Atendida": "verde", "Cancelada": "tenue", "No se presentó": "rojo"}
FASES = ("Higiénica", "Periodoncia", "Operatoria", "Endodoncia", "Cirugía", "Prótesis", "Mantenimiento")
TIPOS_FALLA = ("Necesita limpieza", "Necesita reparación", "Fuera de servicio")
AREAS = ("Diagnóstico", "Periodoncia", "Operatoria", "Endodoncia", "Cirugía", "Prótesis", "Odontopediatría", "Otra")
SEXOS = ("Femenino", "Masculino")
TIPOS_FOTO = ("Presencial", "Radiografía")
# Campos de la historia clinica (RF05): nombre de la columna -> como se muestra
HISTORIA = {"nacimiento": "Fecha de nacimiento", "sexo": "Sexo", "direccion": "Dirección",
            "emergencia_nombre": "Contacto de emergencia", "emergencia_telefono": "Teléfono de emergencia",
            "antecedentes": "Antecedentes médicos", "alergias": "Alergias", "medicamentos": "Medicamentos que toma",
            "antecedentes_odontologicos": "Antecedentes odontológicos", "presion": "Presión arterial",
            "examen_extraoral": "Examen extraoral", "examen_intraoral": "Examen intraoral",
            "diagnostico": "Diagnóstico"}
IMAGEN_MAXIMA = 5 * 1024 * 1024  # RNF09: fotos JPG o PNG de hasta 5 MB
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
-- La tabla salas guarda las clinicas (unidades dentales); en_sala es su sala: 1 pregrado, 2 posgrado
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
    cuando timestamptz NOT NULL DEFAULT now(), url text NOT NULL);
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
    numero text NOT NULL, fecha text NOT NULL, url text NOT NULL, estudiante text REFERENCES cuentas,
    cuando timestamptz NOT NULL DEFAULT now(), firmado_por text REFERENCES cuentas, firmado text,
    UNIQUE (tratamiento_id, numero));
-- Grupos de estudiantes: la Facultad crea un codigo (va en un QR) y cada estudiante crea su cuenta con el.
-- El codigo vence, tiene cupo y se puede cerrar; cada cuenta recuerda con que grupo se registro
CREATE TABLE IF NOT EXISTS invitaciones (
    id serial PRIMARY KEY, nombre text NOT NULL, codigo text NOT NULL UNIQUE, cupo int NOT NULL CHECK (cupo > 0),
    vence timestamptz NOT NULL, cerrado boolean NOT NULL DEFAULT false,
    cuando timestamptz NOT NULL DEFAULT now());
-- Quien creo cada grupo ya queda en la bitacora; con esta columna cuentas e invitaciones se apuntaban
-- la una a la otra (una referencia circular), asi que se quita
ALTER TABLE invitaciones DROP COLUMN IF EXISTS creado_por;
ALTER TABLE cuentas ADD COLUMN IF NOT EXISTS invitacion_id int REFERENCES invitaciones;
-- Decision del profesor sobre el paciente: quien y cuando la tomo y, si lo rechazo, la observacion para el estudiante
ALTER TABLE pacientes ADD COLUMN IF NOT EXISTS rechazo text NOT NULL DEFAULT '',
                      ADD COLUMN IF NOT EXISTS decision_por text REFERENCES cuentas,
                      ADD COLUMN IF NOT EXISTS decision_cuando timestamptz;
-- La cita guarda su estado: una cita cancelada o a la que el paciente no llego queda como historial, no se borra
ALTER TABLE citas ADD COLUMN IF NOT EXISTS estado text NOT NULL DEFAULT 'Programada'
                      CHECK (estado IN ('Programada', 'Confirmada', 'Atendida', 'Cancelada', 'No se presentó')),
                  ADD COLUMN IF NOT EXISTS motivo_cancelacion text NOT NULL DEFAULT '';
-- Las fotos ya no van dentro de la base: se guarda la direccion del archivo (ver mover_imagenes)
ALTER TABLE fotos ADD COLUMN IF NOT EXISTS url text;
ALTER TABLE recibos ADD COLUMN IF NOT EXISTS url text;
-- RF05: radiografias junto a las fotos presenciales
ALTER TABLE fotos ADD COLUMN IF NOT EXISTS tipo text NOT NULL DEFAULT 'Presencial'
                  CHECK (tipo IN ('Presencial', 'Radiografía'));
-- RF03 y RF05: historia clinica, una por paciente. Va en su propia tabla para no seguir ensanchando pacientes
CREATE TABLE IF NOT EXISTS historias (
    paciente_id int PRIMARY KEY REFERENCES pacientes ON DELETE CASCADE,
    nacimiento text, sexo text NOT NULL DEFAULT '' CHECK (sexo IN ('', 'Femenino', 'Masculino')),
    direccion text NOT NULL DEFAULT '', emergencia_nombre text NOT NULL DEFAULT '',
    emergencia_telefono text NOT NULL DEFAULT '', antecedentes text NOT NULL DEFAULT '',
    alergias text NOT NULL DEFAULT '', medicamentos text NOT NULL DEFAULT '',
    antecedentes_odontologicos text NOT NULL DEFAULT '', presion text NOT NULL DEFAULT '',
    examen_extraoral text NOT NULL DEFAULT '', examen_intraoral text NOT NULL DEFAULT '',
    diagnostico text NOT NULL DEFAULT '', actualizado timestamptz, actualizado_por text REFERENCES cuentas);
-- RF09: fase de cada tratamiento e indicaciones al terminar cada cita
ALTER TABLE tratamientos ADD COLUMN IF NOT EXISTS fase text NOT NULL DEFAULT 'Operatoria'
      CHECK (fase IN ('Higiénica', 'Periodoncia', 'Operatoria', 'Endodoncia', 'Cirugía', 'Prótesis', 'Mantenimiento'));
ALTER TABLE citas ADD COLUMN IF NOT EXISTS indicaciones text NOT NULL DEFAULT '';
-- RF14: que le pasa a la clinica
ALTER TABLE reportes ADD COLUMN IF NOT EXISTS tipo text NOT NULL DEFAULT 'Necesita reparación'
      CHECK (tipo IN ('Necesita limpieza', 'Necesita reparación', 'Fuera de servicio'));
-- RF15: codigo y area de cada procedimiento, y los precios que tuvo antes
ALTER TABLE tarifario ADD COLUMN IF NOT EXISTS codigo text UNIQUE,
                      ADD COLUMN IF NOT EXISTS area text NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS precios_anteriores (
    id serial PRIMARY KEY, procedimiento_id int NOT NULL REFERENCES tarifario ON DELETE CASCADE,
    precio numeric(10, 2) NOT NULL, hasta timestamptz NOT NULL DEFAULT now(), cambiado_por text REFERENCES cuentas);
-- RF16: orden de cobro para caja; cada tratamiento entra en una sola orden
CREATE TABLE IF NOT EXISTS ordenes (
    id serial PRIMARY KEY, paciente_id int NOT NULL REFERENCES pacientes ON DELETE CASCADE,
    total numeric(10, 2) NOT NULL CHECK (total > 0), creada timestamptz NOT NULL DEFAULT now(),
    creada_por text REFERENCES cuentas, pagada timestamptz, cobrada_por text REFERENCES cuentas);
ALTER TABLE tratamientos ADD COLUMN IF NOT EXISTS orden_id int REFERENCES ordenes ON DELETE SET NULL;
-- RF17: lo que piden los estudiantes a proveeduria
CREATE TABLE IF NOT EXISTS solicitudes (
    id serial PRIMARY KEY, material_id int NOT NULL REFERENCES materiales ON DELETE CASCADE,
    cantidad int NOT NULL CHECK (cantidad > 0), estudiante text NOT NULL REFERENCES cuentas, para text NOT NULL,
    estado text NOT NULL DEFAULT 'Pendiente' CHECK (estado IN ('Pendiente', 'Entregada', 'Rechazada')),
    cuando timestamptz NOT NULL DEFAULT now(), atendida_por text REFERENCES cuentas, nota text NOT NULL DEFAULT '');
-- RNF07: un paciente eliminado no se borra: se marca y deja de verse; la Facultad lo puede restaurar
ALTER TABLE pacientes ADD COLUMN IF NOT EXISTS eliminado timestamptz,
                      ADD COLUMN IF NOT EXISTS eliminado_por text REFERENCES cuentas,
                      ADD COLUMN IF NOT EXISTS eliminado_motivo text NOT NULL DEFAULT '';
-- Indices para las llaves foraneas que mas se consultan
CREATE INDEX IF NOT EXISTS citas_paciente ON citas (paciente_id);
CREATE INDEX IF NOT EXISTS citas_fecha ON citas (fecha, hora);
CREATE INDEX IF NOT EXISTS pacientes_estudiante ON pacientes (estudiante);
CREATE INDEX IF NOT EXISTS tratamientos_paciente ON tratamientos (paciente_id);
CREATE INDEX IF NOT EXISTS fotos_paciente ON fotos (paciente_id);
CREATE INDEX IF NOT EXISTS abonos_paciente ON abonos (paciente_id);
CREATE INDEX IF NOT EXISTS solicitudes_estado ON solicitudes (estado);
-- RF02: el correo y el año de la carrera de cada cuenta (el estudiante los escribe al registrarse)
ALTER TABLE cuentas ADD COLUMN IF NOT EXISTS correo text NOT NULL DEFAULT '',
                    ADD COLUMN IF NOT EXISTS anio int CHECK (anio BETWEEN 1 AND 6);
-- RF06/RNF06: el profesor a cargo de cada estudiante. Es una relacion uno a muchos dentro de cuentas
-- (un profesor, muchos estudiantes); el profesor ve solo a los pacientes de sus estudiantes
ALTER TABLE cuentas ADD COLUMN IF NOT EXISTS profesor text REFERENCES cuentas ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS cuentas_profesor ON cuentas (profesor);
-- RF10: los turnos clinicos de cada estudiante (dia 0 = lunes ... 5 = sabado, horas HH:MM).
-- Si el estudiante tiene turnos, sus citas tienen que caer dentro de uno
CREATE TABLE IF NOT EXISTS turnos (
    id serial PRIMARY KEY, estudiante text NOT NULL REFERENCES cuentas ON DELETE CASCADE,
    dia int NOT NULL CHECK (dia BETWEEN 0 AND 5), desde text NOT NULL, hasta text NOT NULL,
    CHECK (desde < hasta), UNIQUE (estudiante, dia, desde));
"""

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
CUPO_MAXIMO, DIAS_MAXIMO = 200, 30  # de un grupo: cuántos estudiantes se registran con su QR y cuánto dura
CIF_MAXIMO, NOMBRE_MAXIMO = 20, 80  # caracteres de lo que escribe el estudiante al registrarse
FOTO_LADO = 1600  # pixeles del lado mas largo de la foto presencial guardada
DIAS = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado")  # turnos.dia: 0 a 5
ANIOS = range(1, 7)  # año de la carrera
CORREO = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


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
        if "FLY_APP_NAME" in os.environ and not ALMACEN["bucket"]:
            # En Fly el disco se borra en cada reinicio: sin un almacenamiento de verdad las fotos se perderian.
            # Fallar aqui detiene el despliegue y la version anterior sigue funcionando
            raise SystemExit("Falta configurar el almacenamiento de fotos: ALMACEN_BUCKET, ALMACEN_ENDPOINT,"
                             " ALMACEN_ID y ALMACEN_CLAVE (fly secrets set ...).")
        sql(TABLAS)
        mover_imagenes()
        for cif, (nombre, rol) in CUENTAS_INICIALES.items():
            sql("INSERT INTO cuentas VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                cif, nombre, rol, generate_password_hash(CLAVE_INICIAL))
        # Las clínicas se completan una sola vez, hasta que existe la última. Una base que nació con 20
        # (10 y 10) conserva las suyas con sus citas y reportes: de la 11 a la 20 pasan a pregrado
        if not sql("SELECT 1 FROM salas WHERE nombre = %s", f"Clínica {PREGRADO + POSGRADO}").fetchone():
            with bd().transaction():
                sql("UPDATE salas SET en_sala = 1 WHERE nombre = ANY(%s)",
                    [f"Clínica {n}" for n in range(1, PREGRADO + 1)])
                for n in range(1, PREGRADO + POSGRADO + 1):
                    sql("INSERT INTO salas (nombre, disponible, en_sala) VALUES (%s, %s, %s)"
                        " ON CONFLICT (nombre) DO NOTHING", f"Clínica {n}", n != 3, 1 if n <= PREGRADO else 2)

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
    otro = sql("SELECT eliminado FROM pacientes WHERE regexp_replace(upper(cedula), '[^0-9A-Z]', '', 'g') = %s"
               " AND id <> %s", sin_signos(cedula), pid or 0).fetchone()
    if otro and otro["eliminado"]:
        return "Esa cédula es de un paciente eliminado. Pida a la Facultad que lo restaure."
    if otro:
        return "Ya existe otro paciente con esa cédula." if pid else "Ya existe un paciente con esa cédula."
    return None

def error_de_cita(fecha, hora, sala, paciente_id, estudiante, cita_id=None):
    """Por qué no se puede agendar la cita; None si se puede. Fecha aaaa-mm-dd del calendario, hora HH:MM.
    Ni la clínica, ni el paciente, ni el estudiante pueden tener otra cita a la misma hora (RF10); las canceladas
    no cuentan, y al reprogramar tampoco la cita misma (cita_id)."""
    try:
        cuando = datetime.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M").replace(tzinfo=MANAGUA)
    except ValueError:
        return "Elija la fecha en el calendario y la hora de la cita."
    if cuando < datetime.now(MANAGUA):
        return "No se pueden agendar citas en una fecha u hora que ya pasó."
    if sala and sql("SELECT 1 FROM salas WHERE nombre = %s AND NOT disponible", sala).fetchone():
        return "Esa clínica no está disponible."
    otra = ("SELECT 1 FROM citas c JOIN pacientes p ON p.id = c.paciente_id WHERE c.fecha = %s AND c.hora = %s"
            " AND c.estado <> 'Cancelada' AND p.eliminado IS NULL AND c.id <> %s AND ")
    if sala and sql(otra + "c.sala = %s", fecha, hora, cita_id or 0, sala).fetchone():
        return "Esa clínica ya está ocupada a esa hora."
    if sql(otra + "c.paciente_id = %s", fecha, hora, cita_id or 0, paciente_id).fetchone():
        return "El paciente ya tiene otra cita a esa hora."
    if estudiante and sql(otra + "p.estudiante = %s", fecha, hora, cita_id or 0, estudiante).fetchone():
        return "Usted ya tiene otra cita a esa hora con otro paciente."
    turnos = turnos_de(estudiante) if estudiante else []
    if turnos and not any(t["dia"] == cuando.weekday() and t["desde"] <= hora < t["hasta"] for t in turnos):
        return "Esa hora está fuera de sus turnos clínicos: " + ", ".join(
            f"{DIAS[t['dia']]} de {t['desde']} a {t['hasta']}" for t in turnos) + "."
    return None

def turnos_de(estudiante):
    """RF10: los turnos clinicos del estudiante, por dia y hora."""
    return sql("SELECT * FROM turnos WHERE estudiante = %s ORDER BY dia, desde", estudiante).fetchall()

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

def mover_imagenes():
    """Las bases de versiones anteriores guardaban las fotos dentro (bytea). Las sube al almacenamiento, deja su
    direccion en url y, cuando ya no queda ninguna adentro, quita la columna vieja. Si se corta a la mitad,
    la proxima vez sigue donde quedo."""
    for tabla, vieja in (("fotos", "imagen"), ("recibos", "foto")):
        if sql("SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()"
               " AND table_name = %s AND column_name = %s", tabla, vieja).fetchone():
            sql(f"ALTER TABLE {tabla} ALTER COLUMN {vieja} DROP NOT NULL")
            for fila in sql(f"SELECT id, {vieja} AS datos FROM {tabla} WHERE url IS NULL").fetchall():
                sql(f"UPDATE {tabla} SET url = %s, {vieja} = NULL WHERE id = %s",
                    guardar_archivo(tabla, bytes(fila["datos"])), fila["id"])
            sql(f"ALTER TABLE {tabla} DROP COLUMN {vieja}")
        sql(f"ALTER TABLE {tabla} ALTER COLUMN url SET NOT NULL")


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
                     " (SELECT count(*) FROM materiales WHERE cantidad <= minimo) AS inventario,"
                     " (SELECT count(*) FROM solicitudes WHERE estado = 'Pendiente') AS solicitudes").fetchone()
    elif rol == "profesor":  # y el profesor, cuantos recibos esperan su firma y cuantos pacientes su decision
        avisos = sql("SELECT (SELECT count(*) FROM recibos WHERE firmado IS NULL AND " + de_mi_grupo("recibos.estudiante")
                     + ") AS control, (SELECT count(*) FROM pacientes WHERE NOT aprobado AND rechazo = ''"
                     " AND finalizado IS NULL AND eliminado IS NULL AND " + de_mi_grupo("pacientes.estudiante")
                     + ") AS pacientes", cif, cif).fetchone()
    else:  # y el estudiante, cuantos de sus pacientes rechazo el profesor (RF04: el aviso al estudiante)
        avisos = sql("SELECT count(*) AS pacientes FROM pacientes WHERE estudiante = %s AND rechazo <> ''"
                     " AND finalizado IS NULL AND eliminado IS NULL", cif).fetchone()
    pestanas = [(f"{t} ({avisos[e]})" if avisos.get(e) else t, e) for t, e in MENUS[rol]
                if e != "comentarios" or COMENTARIOS]
    return {"cif": cif, "nombre": nombre, "rol": ROLES[rol], "es_rol": rol, "pestanas": pestanas,
            "fallas": avisos.get("salas", 0), "bajos": avisos.get("inventario", 0),
            "solicitudes_pendientes": avisos.get("solicitudes", 0)}

def visibles(alias="pacientes"):
    """Condicion SQL (y sus argumentos) de los pacientes que el usuario puede ver. El estudiante, los suyos;
    el profesor, los de sus estudiantes (RF06/RNF06), y los de estudiantes que todavia no tienen profesor
    asignado, para que nadie quede sin revisar; la Facultad, todos."""
    cif, _, rol = quien()
    if rol == "estudiante":
        return f"{alias}.estudiante = %s", [cif]
    if rol == "profesor":
        return de_mi_grupo(f"{alias}.estudiante"), [cif]
    return "true", []

def de_mi_grupo(columna):
    """Condicion SQL: el estudiante de `columna` no esta a cargo de otro profesor (argumento: el profesor)."""
    return f"NOT EXISTS (SELECT 1 FROM cuentas e WHERE e.cif = {columna} AND e.profesor <> %s)"

def paciente(pid):
    """El paciente, solo si el usuario puede verlo: cada estudiante ve unicamente los suyos.
    Si la Facultad le dio de alta, solo ella puede cambiar algo (por ejemplo, un abono)."""
    cif, _, rol = quien()
    condicion, args = visibles()
    p = sql("SELECT * FROM pacientes WHERE id = %s AND eliminado IS NULL AND " + condicion, pid, *args).fetchone()
    if p and p["finalizado"] and rol != "admin" and request.method == "POST":
        flash(f"{p['nombre']} ya recibió el alta: su expediente ya no se puede cambiar.", "error")
        return None
    return p

def mis_pacientes(buscar=""):
    """Los pacientes que el usuario puede ver; con `buscar`, solo los que coinciden en parte con el nombre, la
    cédula (con o sin guiones) o el número de expediente (RF06). Trae cuántas veces no llegó a una cita."""
    # La firma solo importa para saber si ya firmo; no hace falta traer la imagen
    condicion, args = visibles()
    texto = "%" + re.sub(r"([\\%_])", r"\\\1", buscar.strip()) + "%"
    cedula = "%" + sin_signos(buscar) + "%" if sin_signos(buscar) else ""
    return sql("SELECT id, nombre, cedula, telefono, expediente, razon, estudiante, aprobado, rechazo,"
               " firma <> '' AS firma, finalizado, (SELECT count(*) FROM citas c WHERE c.paciente_id = pacientes.id"
               " AND c.estado = 'No se presentó') AS inasistencias"
               " FROM pacientes WHERE eliminado IS NULL AND " + condicion +
               " AND (%s = '' OR nombre ILIKE %s OR expediente ILIKE %s"
               " OR regexp_replace(upper(cedula), '[^0-9A-Z]', '', 'g') LIKE %s) ORDER BY id",
               *args, buscar.strip(), texto, texto, cedula).fetchall()

def nombre_de(cif):
    return cuentas()[cif][0] if cif in cuentas() else "sin asignar"

def todas_las_salas():
    """Las clinicas, ordenadas por sala y por su número (así salen en su lugar del plano), y si tienen una
    falla sin solucionar, su motivo y quién la reportó."""
    return sql("SELECT s.*, r.id AS reporte_id, r.motivo, r.tipo, r.cif AS reporto,"
               " to_char(r.cuando AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS reportada"
               " FROM salas s LEFT JOIN reportes r ON r.sala_id = s.id AND r.resuelto_cuando IS NULL"
               " ORDER BY s.en_sala, substring(s.nombre FROM '[0-9]+')::numeric NULLS LAST, s.id").fetchall()


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
                session.permanent = True  # cuenta los 15 min desde la ultima pagina que se abrio
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
    cif, _, rol = quien()
    condicion, args = visibles()
    revisar = sql("SELECT id, nombre, estudiante FROM pacientes WHERE odonto_revision = 'pedida'"
                  " AND eliminado IS NULL AND " + condicion + " ORDER BY id", *args).fetchall() if rol == "profesor" else []
    rechazados = sql("SELECT id, nombre, rechazo, decision_por FROM pacientes WHERE estudiante = %s AND rechazo <> ''"
                     " AND finalizado IS NULL AND eliminado IS NULL ORDER BY id", cif).fetchall() if rol == "estudiante" else []
    # RF13: la jornada de hoy segun el rol. El estudiante ve sus citas; el profesor y la Facultad, todas
    de_quien, args_agenda = visibles("p")
    agenda = ("SELECT c.*, p.nombre AS paciente, p.telefono, p.estudiante FROM citas c JOIN pacientes p ON p.id = c.paciente_id"
              " WHERE p.eliminado IS NULL AND c.fecha = %s AND c.estado <> 'Cancelada' AND " + de_quien + " ORDER BY c.hora")
    manana = (datetime.now(MANAGUA) + timedelta(days=1)).date().isoformat()
    por_aprobar = sql("SELECT id, nombre, estudiante FROM pacientes WHERE NOT aprobado AND rechazo = ''"
                      " AND finalizado IS NULL AND eliminado IS NULL AND " + condicion + " ORDER BY id",
                      *args).fetchall() if rol == "profesor" else []
    clinicas = sql("SELECT count(*) FILTER (WHERE disponible) AS disponibles, count(*) FILTER (WHERE NOT disponible)"
                   " AS no_disponibles FROM salas").fetchone()
    return render_template("menu.html", **comun(), que_hace=QUE_HACE[rol], revisar=revisar, rechazados=rechazados,
                           nombre_de=nombre_de, citas_hoy=sql(agenda, hoy(), *args_agenda).fetchall(),
                           turnos=turnos_de(cif) if rol == "estudiante" else [], dias=DIAS,
                           # RF12: las citas de manana que el paciente todavia no confirma, para recordarselas
                           sin_confirmar=[c for c in sql(agenda, manana, *args_agenda).fetchall() if c["estado"] == "Programada"]
                           if rol == "estudiante" else [], por_aprobar=por_aprobar, clinicas=clinicas)


@app.route("/pacientes", methods=["GET", "POST"])
def pacientes():
    cif, _, rol = quien()
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
                    pid = sql("INSERT INTO pacientes (nombre, cedula, telefono, expediente, estudiante)"
                              " VALUES (%s, %s, %s, %s, %s) RETURNING id", form["nombre"], form["cedula"],
                              form["telefono"], form["expediente"], cif).fetchone()["id"]
                    sql("INSERT INTO trabajo (paciente_id, estudiante, desde) VALUES (%s, %s, %s)", pid, cif, hoy())
                    # RF03: si no trae numero de expediente fisico, se le da uno correlativo
                    sql("UPDATE pacientes SET expediente = 'EXP-' || lpad(id::text, 5, '0') WHERE id = %s"
                        " AND expediente = ''", pid)
                    sql("INSERT INTO historias (paciente_id, nacimiento, sexo, direccion, emergencia_nombre,"
                        " emergencia_telefono, actualizado, actualizado_por) VALUES (%s, %s, %s, %s, %s, %s, now(), %s)",
                        pid, form["nacimiento"] or None, form["sexo"], form["direccion"], form["emergencia_nombre"],
                        form["emergencia_telefono"], cif)
                    anotar(f"Registró al paciente {form['nombre']} ({form['cedula']})")
                # Paso siguiente: la razon por la que esta el paciente
                return redirect(url_for("razon", pid=pid))
        elif accion in ("aprobar", "rechazar") and rol == "profesor":  # el profesor solo revisa
            p = paciente(request.form.get("id", type=int))
            observacion = request.form.get("observacion", "").strip()
            if not p:
                flash("Ese paciente no existe.", "error")
            elif accion == "rechazar" and not observacion:
                flash("Para rechazar al paciente escriba la observación para el estudiante.", "error")
            elif accion == "rechazar" and len(observacion) > MOTIVO_MAXIMO:
                flash(f"La observación puede tener hasta {MOTIVO_MAXIMO} caracteres.", "error")
            else:
                # Aprobar, rechazar o quitar la aprobación: queda quién lo decidió y cuándo (RF04)
                aprobado = accion == "aprobar" and request.form.get("aprobado") == "1"
                sql("UPDATE pacientes SET aprobado = %s, rechazo = %s, decision_por = %s, decision_cuando = now()"
                    " WHERE id = %s", aprobado, observacion if accion == "rechazar" else "", cif, p["id"])
                if accion == "rechazar":
                    anotar(f"Rechazó a {p['nombre']}: {observacion}")
                    flash(f"{p['nombre']} rechazado. El estudiante verá la observación.", "ok")
                else:
                    anotar(("Aprobó" if aprobado else "Retiró la aprobación de") + f" a {p['nombre']}")
                    flash("Paciente aprobado." if aprobado else "Aprobación retirada.", "ok")
        elif accion == "restaurar" and rol == "admin":
            r = sql("UPDATE pacientes SET eliminado = NULL, eliminado_por = NULL, eliminado_motivo = '' WHERE id = %s"
                    " AND eliminado IS NOT NULL RETURNING nombre", request.form.get("id", type=int)).fetchone()
            if r:
                anotar(f"Restauró al paciente {r['nombre']}")
                flash(f"{r['nombre']} restaurado con todo su expediente.", "ok")
    buscar = request.args.get("buscar", "").strip()
    eliminados = sql("SELECT id, nombre, cedula, eliminado_motivo, eliminado_por, to_char(eliminado AT TIME ZONE"
                     " 'America/Managua', 'DD/MM/YYYY') AS cuando FROM pacientes WHERE eliminado IS NOT NULL"
                     " ORDER BY eliminado DESC").fetchall() if rol == "admin" else []
    return render_template("pacientes.html", **comun(), pacientes=mis_pacientes(buscar), buscar=buscar, form=form,
                           nombre_de=nombre_de, sexos=SEXOS, eliminados=eliminados)


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
        error = (fecha or hora) and error_de_cita(fecha, hora, sala, pid, p["estudiante"])
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
        if error := error_de_paciente(form, pid):
            flash(error, "error")
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
    citas = sql("SELECT * FROM citas WHERE paciente_id = %s", pid).fetchall()
    h = historia_de(pid)
    return render_template("expediente.html", **comun(), p=p, nombre_de=nombre_de, revisiones=REVISIONES, citas=citas,
                           color_cita=COLOR_CITA, inasistencias=sum(c["estado"] == "No se presentó" for c in citas),
                           h=h, alertas=alertas_de(h), avance=avance_de(pid))


def historia_de(pid):
    return sql("SELECT * FROM historias WHERE paciente_id = %s", pid).fetchone() or {c: "" for c in HISTORIA}

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
    """Cuanto del plan esta realizado (RF09): realizados, planificados y porcentaje."""
    a = sql("SELECT count(*) FILTER (WHERE realizado IS NOT NULL) AS hechos, count(*) AS total FROM tratamientos"
            " WHERE paciente_id = %s", pid).fetchone()
    a["porcentaje"] = round(100 * a["hechos"] / a["total"]) if a["total"] else 0
    return a


@app.route("/pacientes/<int:pid>/historia", methods=["GET", "POST"])
def historia(pid):
    """Historia clinica digital (RF05): la llena el estudiante a cargo y se puede guardar a medias para
    seguir en la proxima cita. Quien la cambio y que campos queda en la bitacora (historial de cambios)."""
    p = paciente(pid)
    if not p:
        return redirect(url_for("pacientes"))
    cif, _, rol = quien()
    h = historia_de(pid)
    if request.method == "POST" and rol == "estudiante":
        form = {c: request.form.get(c, "").strip() for c in HISTORIA}
        if error := error_de_historia(form):
            flash(error, "error")
            h = {**h, **form}
        else:
            cambiados = [HISTORIA[c] for c in HISTORIA if (str(h.get(c) or "")) != form[c]]
            form["nacimiento"] = form["nacimiento"] or None
            columnas = list(HISTORIA)
            sql("INSERT INTO historias (paciente_id, " + ", ".join(columnas) + ", actualizado, actualizado_por)"
                " VALUES (%s, " + ", ".join(["%s"] * len(columnas)) + ", now(), %s) ON CONFLICT (paciente_id) DO UPDATE SET "
                + ", ".join(f"{c} = excluded.{c}" for c in columnas) + ", actualizado = now(), actualizado_por = %s",
                pid, *[form[c] for c in columnas], cif, cif)
            if cambiados:
                anotar(f"Actualizó la historia clínica de {p['nombre']}: " + ", ".join(cambiados).lower())
            flash("Historia clínica guardada. Puede seguir llenándola en la próxima cita.", "ok")
            return redirect(url_for("historia", pid=pid))
    return render_template("historia.html", **comun(), p=p, h=h, campos=HISTORIA, sexos=SEXOS, alertas=alertas_de(h),
                           hoy=hoy(), nombre_de=nombre_de, puede=rol == "estudiante" and not p["finalizado"])


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
            if sin_signos(request.form.get("cedula")) != sin_signos(p["cedula"]) or not texto:
                flash("Para eliminarlo escriba la cédula del paciente y el motivo.", "error")
            elif any(r["firmado"] for r in recibos_de(paciente_id=pid)):
                flash(f"{p['nombre']} tiene recibos de pago firmados por el docente: no se puede eliminar."
                      " Si terminó su tratamiento, dele de alta.", "error")
            else:
                # RNF07: no se borra nada; deja de verse y la Facultad lo puede restaurar desde Pacientes
                sql("UPDATE pacientes SET eliminado = now(), eliminado_por = %s, eliminado_motivo = %s WHERE id = %s",
                    cif, texto, pid)
                anotar(f"Eliminó al paciente {p['nombre']} ({p['cedula']}): {texto}")
                flash(f"{p['nombre']} fue eliminado. Su expediente se conserva y se puede restaurar.", "ok")
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
    accion = request.form.get("accion")
    if request.method == "POST" and rol == "estudiante" and accion in ("cancelar", "estado", "reprogramar"):
        cita = sql("SELECT * FROM citas WHERE id = %s", request.form.get("id", type=int)).fetchone()
        p = cita and paciente(cita["paciente_id"])
        if p:
            cambiar_cita(cita, p, accion)
        return redirect(url_for("citas"))
    if request.method == "POST" and rol == "estudiante":
        p = paciente(request.form.get("paciente_id", type=int))
        f = {c: request.form.get(c, "").strip() for c in ("fecha", "hora", "sala", "razon")}
        if not (p and f["fecha"] and f["hora"] and f["razon"]):
            flash("Elige paciente, fecha, hora y la razón de la cita.", "error")
        elif error := error_de_cita(f["fecha"], f["hora"], f["sala"], p["id"], p["estudiante"]):
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
                           mios=mios, nombre_de=nombre_de, activas=ACTIVAS, color_cita=COLOR_CITA)


def cambiar_cita(cita, p, accion):
    """El estudiante a cargo confirma, marca como atendida o como no se presentó, reprograma o cancela una cita
    (RF11). Nada se borra: la cita queda en el historial del paciente con su estado."""
    cuando = f"{dmy(cita['fecha'])} a las {cita['hora']}"
    nuevo = "Cancelada" if accion == "cancelar" else request.form.get("estado", "")
    if cita["estado"] not in ACTIVAS:
        flash(f"Esa cita ya está {cita['estado'].lower()}: ya no se cambia.", "error")
    elif accion == "reprogramar":
        f = {c: request.form.get(c, "").strip() for c in ("fecha", "hora", "sala")}
        if cita["fecha"] < hoy():
            flash("Esa cita ya pasó: márquela como atendida o como que no se presentó.", "error")
        elif error := error_de_cita(f["fecha"], f["hora"], f["sala"], p["id"], p["estudiante"], cita["id"]):
            flash(error, "error")
        else:
            sql("UPDATE citas SET fecha = %s, hora = %s, sala = %s, estado = 'Programada' WHERE id = %s",
                f["fecha"], f["hora"], f["sala"], cita["id"])
            anotar(f"Reprogramó la cita de {p['nombre']} del {cuando} al {dmy(f['fecha'])} a las {f['hora']}")
            flash("Cita reprogramada.", "ok")
    elif nuevo not in SIGUIENTE[cita["estado"]]:
        flash("Elija qué pasó con la cita.", "error")
    elif nuevo in ("Confirmada", "Cancelada") and cita["fecha"] < hoy():
        # Las citas pasadas no se cancelan ni se confirman: son historial
        flash("Esa cita ya pasó: márquela como atendida o como que no se presentó.", "error")
    elif nuevo in ("Atendida", "No se presentó") and cita["fecha"] > hoy():
        flash("Una cita se marca como atendida o como que no se presentó el día de la cita o después.", "error")
    elif nuevo == "Cancelada" and not request.form.get("motivo", "").strip():
        flash("Escriba el motivo de la cancelación.", "error")
    else:
        motivo = request.form.get("motivo", "").strip()[:MOTIVO_MAXIMO] if nuevo == "Cancelada" else ""
        indicaciones = request.form.get("indicaciones", "").strip()[:MOTIVO_MAXIMO] if nuevo == "Atendida" else ""
        sql("UPDATE citas SET estado = %s, motivo_cancelacion = %s, indicaciones = %s WHERE id = %s",
            nuevo, motivo, indicaciones, cita["id"])
        anotar(f"Cita de {p['nombre']} del {cuando}: {nuevo.lower()}" + (f" ({motivo})" if motivo else ""))
        flash(f"Cita {nuevo.lower()}.", "ok")


@app.route("/salas", methods=["GET", "POST"])
def salas():
    """Plano de las clínicas, acomodadas como en las salas reales, con un check si están disponibles o una X si no.
    El estudiante reporta las clínicas que no funcionan y la Facultad las marca como solucionadas."""
    cif, _, rol = quien()
    if request.method == "POST":
        accion = request.form.get("accion")
        if rol == "estudiante" and accion == "reportar":
            s = sql("SELECT id, nombre FROM salas WHERE id = %s", request.form.get("sala", type=int)).fetchone()
            motivo, tipo = request.form.get("motivo", "").strip(), request.form.get("tipo", "Necesita reparación")
            if not s or not motivo or tipo not in TIPOS_FALLA:
                flash("Elija la clínica y escriba qué es lo que no funciona.", "error")
            elif len(motivo) > MOTIVO_MAXIMO:
                flash(f"El motivo puede tener hasta {MOTIVO_MAXIMO} caracteres.", "error")
            else:
                with bd().transaction():
                    nuevo = sql("INSERT INTO reportes (sala_id, cif, motivo, tipo) VALUES (%s, %s, %s, %s)"
                                " ON CONFLICT (sala_id) WHERE resuelto_cuando IS NULL DO NOTHING RETURNING id",
                                s["id"], cif, motivo, tipo).fetchone()
                    if nuevo:
                        sql("UPDATE salas SET disponible = false WHERE id = %s", s["id"])
                        anotar(f"Reportó {s['nombre']} ({tipo.lower()}): {motivo}")
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
    historial = sql("SELECT s.nombre, r.tipo, r.motivo, r.cif, r.resuelto_por,"
                    " to_char(r.cuando AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS cuando,"
                    " to_char(r.resuelto_cuando AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS resuelto"
                    " FROM reportes r JOIN salas s ON s.id = r.sala_id WHERE r.resuelto_cuando IS NOT NULL"
                    " ORDER BY r.resuelto_cuando DESC LIMIT 100").fetchall() if rol != "estudiante" else []
    return render_template("salas.html", **comun(), salas=todas_las_salas(), nombre_de=nombre_de,
                           motivo_maximo=MOTIVO_MAXIMO, tipos_falla=TIPOS_FALLA, historial=historial)


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
                    flash("Elija una foto JPG o PNG de hasta 5 MB, o tómela con la cámara.", "error")
                    return redirect(url_for("odontograma", pid=pid))
                tipo = request.form.get("tipo", "Presencial")
                tipo = tipo if tipo in TIPOS_FOTO else "Presencial"
                que = "una foto presencial" if tipo == "Presencial" else "una radiografía"
                guardar = ("INSERT INTO fotos (paciente_id, cif, url, tipo) VALUES (%s, %s, %s, %s)", pid, cif,
                           guardar_archivo("fotos", imagen), tipo)
            with bd().transaction():
                sql(*guardar)
                # Si ya estaba aprobado y se cambia, el profesor lo tiene que volver a revisar
                sql("UPDATE pacientes SET odonto_revision = '' WHERE id = %s AND odonto_revision = 'aprobada'", pid)
                anotar(f"Anotó en el odontograma de {p['nombre']} {que}")
            flash("Foto guardada." if accion == "foto" else "Diente guardado.", "ok")
        elif rol == "estudiante" and accion == "pedir":
            if not sql("SELECT 1 FROM fotos WHERE paciente_id = %s AND tipo = 'Presencial'", pid).fetchone():
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
        tipos_foto=TIPOS_FOTO,
        fotos=sql("SELECT id, cif, tipo, to_char(cuando AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS cuando"
                  " FROM fotos WHERE paciente_id = %s ORDER BY id DESC", pid).fetchall())


@app.route("/pacientes/<int:pid>/fotos/<int:fid>.jpg")
def foto(pid, fid):
    # Solo la ve quien puede ver al paciente
    f = paciente(pid) and sql("SELECT url FROM fotos WHERE id = %s AND paciente_id = %s", fid, pid).fetchone()
    if not f:
        return redirect(url_for("pacientes"))
    return ver_archivo(f["url"])


def plan_de(pid):
    return sql("SELECT t.*, a.nombre FROM tratamientos t JOIN tarifario a ON a.id = t.procedimiento_id"
               " WHERE t.paciente_id = %s ORDER BY array_position(%s::text[], t.fase), t.id", pid, list(FASES)).fetchall()

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
            fase = request.form.get("fase", "Operatoria")
            if not proc or (diente is not None and diente not in DIENTES) or fase not in FASES:
                flash("Elija el procedimiento del tarifario y la fase.", "error")
            else:
                sql("INSERT INTO tratamientos (paciente_id, procedimiento_id, diente, precio, estudiante, anotado, fase)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)", pid, proc["id"], diente, proc["precio"], cif, hoy(), fase)
                anotar(f"Anotó en el plan de {p['nombre']}: {proc['nombre']}" + (f" del {diente}" if diente else ""))
                flash("Tratamiento anotado en el plan.", "ok")
        elif rol == "estudiante" and accion in ("realizado", "quitar"):
            t = sql("SELECT t.id, t.orden_id, a.nombre FROM tratamientos t JOIN tarifario a ON a.id = t.procedimiento_id"
                    " WHERE t.id = %s AND t.paciente_id = %s AND t.realizado IS NULL",
                    request.form.get("id", type=int), pid).fetchone()
            if t and accion == "realizado" and not p["firma"]:
                flash("Falta la firma del consentimiento informado: sin ella no se realiza ningún tratamiento.", "error")
            elif t and accion == "realizado":
                # queda a nombre de quien lo realizó: a ese estudiante le toca subir el recibo de pago
                sql("UPDATE tratamientos SET realizado = %s, estudiante = %s WHERE id = %s", hoy(), cif, t["id"])
                anotar(f"Realizó a {p['nombre']}: {t['nombre']}")
                flash(f"{t['nombre']} quedó como realizado.", "ok")
            elif t and t["orden_id"]:
                flash(f"{t['nombre']} ya está en la orden de cobro Nº {t['orden_id']}: no se quita.", "error")
            elif t:  # lo pendiente se puede quitar; lo realizado ya es historial
                sql("DELETE FROM tratamientos WHERE id = %s", t["id"])
                anotar(f"Quitó del plan de {p['nombre']}: {t['nombre']}")
                flash(f"{t['nombre']} quitado del plan.", "ok")
        elif rol in ("estudiante", "admin") and accion == "orden":
            # RF16: la orden lleva todo lo del plan que todavia no esta en otra orden, con su precio
            with bd().transaction():
                total = sql("SELECT coalesce(sum(precio), 0) AS total FROM tratamientos WHERE paciente_id = %s"
                            " AND orden_id IS NULL", pid).fetchone()["total"]
                orden = total > 0 and sql("INSERT INTO ordenes (paciente_id, total, creada_por) VALUES (%s, %s, %s)"
                                          " RETURNING id", pid, total, cif).fetchone()["id"]
                if orden:
                    sql("UPDATE tratamientos SET orden_id = %s WHERE paciente_id = %s AND orden_id IS NULL", orden, pid)
                    anotar(f"Generó la orden de cobro Nº {orden} de {p['nombre']} por {cordobas(total)}")
            if not orden:
                flash("No hay tratamientos sin orden de cobro.", "error")
                return redirect(url_for("tratamientos", pid=pid))
            return redirect(url_for("orden", oid=orden))
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
        fases=FASES, avance=avance_de(pid), ordenes=sql(
            "SELECT id, total, pagada, to_char(creada AT TIME ZONE 'America/Managua', 'DD/MM/YYYY') AS creada"
            " FROM ordenes WHERE paciente_id = %s ORDER BY id", pid).fetchall(),
        sin_orden=sql("SELECT count(*) AS n FROM tratamientos WHERE paciente_id = %s AND orden_id IS NULL",
                      pid).fetchone()["n"],
        recibos=recibos_de(paciente_id=pid),
        dientes=ARRIBA + ABAJO,
        tarifario=sql("SELECT * FROM tarifario WHERE activo ORDER BY nombre").fetchall(),
        abonos=sql("SELECT monto, cif, to_char(cuando AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS cuando"
                   " FROM abonos WHERE paciente_id = %s ORDER BY id", pid).fetchall())


@app.route("/ordenes/<int:oid>", methods=["GET", "POST"])
def orden(oid):
    """Orden de cobro para caja (RF16): numero, tratamientos con su precio y total. Se imprime o se muestra en
    pantalla; la Facultad la marca como pagada y eso registra el abono del paciente."""
    o = sql("SELECT * FROM ordenes WHERE id = %s", oid).fetchone()
    p = o and paciente(o["paciente_id"])
    if not p:
        return redirect(url_for("pacientes"))
    cif, _, rol = quien()
    if request.method == "POST" and rol == "admin" and not o["pagada"]:
        with bd().transaction():
            sql("UPDATE ordenes SET pagada = now(), cobrada_por = %s WHERE id = %s", cif, oid)
            sql("INSERT INTO abonos (paciente_id, monto, cif) VALUES (%s, %s, %s)", p["id"], o["total"], cif)
            anotar(f"Cobró la orden Nº {oid} de {p['nombre']}: {cordobas(o['total'])}")
        flash(f"Orden Nº {oid} pagada; el abono quedó registrado.", "ok")
        return redirect(url_for("orden", oid=oid))
    o["pagada_texto"] = o["pagada"] and o["pagada"].astimezone(MANAGUA).strftime("%d/%m/%Y %H:%M")
    o["creada_texto"] = o["creada"].astimezone(MANAGUA).strftime("%d/%m/%Y %H:%M")
    return render_template("orden.html", **comun(), o=o, p=p, nombre_de=nombre_de, lineas=sql(
        "SELECT t.*, a.nombre, a.codigo FROM tratamientos t JOIN tarifario a ON a.id = t.procedimiento_id"
        " WHERE t.orden_id = %s ORDER BY t.id", oid).fetchall())


@app.route("/tarifario", methods=["GET", "POST"])
def tarifario():
    """Los procedimientos y su precio. Lo lleva la Facultad; un procedimiento desactivado ya no se anota."""
    if quien()[2] != "admin":
        return redirect(url_for("menu"))
    if request.method == "POST":
        accion, precio = request.form.get("accion"), monto(request.form.get("precio"))
        if accion == "agregar":
            nombre = request.form.get("nombre", "").strip()
            codigo = request.form.get("codigo", "").strip().upper() or None
            area = request.form.get("area", "")
            if not nombre or precio is None or precio <= 0:
                flash("Escriba el procedimiento y su precio, que tiene que ser mayor que cero.", "error")
            elif area not in ("", *AREAS):
                flash("Elija el área clínica de la lista.", "error")
            elif sql("SELECT 1 FROM tarifario WHERE lower(nombre) = lower(%s)", nombre).fetchone():
                flash("Ese procedimiento ya está en el tarifario.", "error")
            elif codigo and sql("SELECT 1 FROM tarifario WHERE codigo = %s", codigo).fetchone():
                flash("Ese código ya es de otro procedimiento.", "error")
            else:
                sql("INSERT INTO tarifario (nombre, precio, codigo, area) VALUES (%s, %s, %s, %s)",
                    nombre, precio, codigo, area)
                anotar(f"Agregó al tarifario {nombre} a {cordobas(precio)}")
                flash(f"{nombre} agregado al tarifario.", "ok")
        elif accion == "precio":
            antes = sql("SELECT precio FROM tarifario WHERE id = %s", request.form.get("id", type=int)).fetchone()
            fila = None
            if precio is not None and precio > 0 and antes and antes["precio"] != precio:
                with bd().transaction():  # el precio anterior queda en el historial
                    sql("INSERT INTO precios_anteriores (procedimiento_id, precio, cambiado_por) VALUES (%s, %s, %s)",
                        request.form.get("id", type=int), antes["precio"], quien()[0])
                    fila = sql("UPDATE tarifario SET precio = %s WHERE id = %s RETURNING nombre",
                               precio, request.form.get("id", type=int)).fetchone()
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
    for a in sql("SELECT procedimiento_id, precio, cambiado_por, to_char(hasta AT TIME ZONE 'America/Managua',"
                 " 'DD/MM/YYYY') AS hasta FROM precios_anteriores ORDER BY id DESC").fetchall():
        anteriores.setdefault(a["procedimiento_id"], []).append(a)
    return render_template("tarifario.html", **comun(), areas=AREAS, anteriores=anteriores, nombre_de=nombre_de,
                           procedimientos=sql("SELECT * FROM tarifario ORDER BY area, nombre").fetchall())


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
        elif accion in ("entregar", "rechazar"):
            atender_solicitud(request.form.get("id", type=int), accion)
        return redirect(url_for("inventario"))
    return render_template("inventario.html", **comun(), nombre_de=nombre_de, solicitudes=solicitudes_de(), materiales=sql(
        "SELECT *, cantidad <= minimo AS bajo FROM materiales ORDER BY cantidad <= minimo DESC, nombre").fetchall())


def solicitudes_de(estudiante=None):
    return sql("SELECT s.*, m.nombre AS material, m.unidad, m.cantidad AS hay,"
               " to_char(s.cuando AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS pedida FROM solicitudes s"
               " JOIN materiales m ON m.id = s.material_id WHERE (%s::text IS NULL OR s.estudiante = %s)"
               " ORDER BY s.estado <> 'Pendiente', s.id DESC LIMIT 200", estudiante, estudiante).fetchall()

def atender_solicitud(sid, accion):
    """La Facultad entrega lo pedido (se descuenta del inventario) o lo rechaza con una nota."""
    cif = quien()[0]
    s = sql("SELECT s.*, m.nombre, m.unidad FROM solicitudes s JOIN materiales m ON m.id = s.material_id"
            " WHERE s.id = %s AND s.estado = 'Pendiente'", sid).fetchone()
    if not s:
        return
    if accion == "rechazar":
        nota = request.form.get("nota", "").strip()[:MOTIVO_MAXIMO]
        if not nota:
            flash("Escriba por qué se rechaza la solicitud.", "error")
            return
        sql("UPDATE solicitudes SET estado = 'Rechazada', atendida_por = %s, nota = %s WHERE id = %s", cif, nota, sid)
        anotar(f"Rechazó la solicitud de {s['cantidad']} {s['unidad']} de {s['nombre']} de {nombre_de(s['estudiante'])}: {nota}")
        flash("Solicitud rechazada.", "ok")
        return
    with bd().transaction():
        quedan = sql("UPDATE materiales SET cantidad = cantidad - %s WHERE id = %s AND cantidad >= %s RETURNING cantidad",
                     s["cantidad"], s["material_id"], s["cantidad"]).fetchone()
        if quedan:
            sql("UPDATE solicitudes SET estado = 'Entregada', atendida_por = %s WHERE id = %s", cif, sid)
            anotar(f"Entregó {s['cantidad']} {s['unidad']} de {s['nombre']} a {nombre_de(s['estudiante'])};"
                   f" quedan {quedan['cantidad']}")
    if quedan:
        flash(f"Entregado. Quedan {quedan['cantidad']} {s['unidad']} de {s['nombre']}.", "ok")
    else:
        flash(f"No hay suficiente {s['nombre']} para entregar lo pedido.", "error")


@app.route("/materiales", methods=["GET", "POST"])
def materiales():
    """El estudiante ve que materiales hay y pide lo que necesita para su cita (RF17)."""
    cif, _, rol = quien()
    if rol != "estudiante":
        return redirect(url_for("inventario" if rol == "admin" else "menu"))
    if request.method == "POST":
        m = sql("SELECT id, nombre, unidad FROM materiales WHERE id = %s", request.form.get("material", type=int)).fetchone()
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
            sql("INSERT INTO solicitudes (material_id, cantidad, estudiante, para) VALUES (%s, %s, %s, %s)",
                m["id"], cantidad, cif, para)
            anotar(f"Pidió {cantidad} {m['unidad']} de {m['nombre']} para el {dmy(para)}")
            flash("Solicitud enviada a proveeduría.", "ok")
        return redirect(url_for("materiales"))
    return render_template("materiales.html", **comun(), hoy=hoy(), solicitudes=solicitudes_de(cif), nombre_de=nombre_de,
                           materiales=sql("SELECT *, cantidad <= minimo AS bajo FROM materiales ORDER BY nombre").fetchall())


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
            correo, anio = request.form.get("correo", "").strip(), request.form.get("anio", type=int)
            if not cif.isalnum() or not nombre or rol_nuevo not in ROLES:
                flash("Escriba el CIF (solo números y letras), el nombre y el rol.", "error")
            elif correo and not CORREO.fullmatch(correo):
                flash("Ese correo no es válido.", "error")
            elif len(clave) < CLAVE_MINIMA:
                flash(f"La contraseña debe tener al menos {CLAVE_MINIMA} caracteres.", "error")
            elif cif in cuentas():
                flash("Ya existe una cuenta con ese CIF.", "error")
            else:
                sql("INSERT INTO cuentas (cif, nombre, rol, clave, correo, anio) VALUES (%s, %s, %s, %s, %s, %s)",
                    cif, nombre, rol_nuevo, generate_password_hash(clave), correo,
                    anio if rol_nuevo == "estudiante" and anio in ANIOS else None)
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
                gid = sql("INSERT INTO invitaciones (nombre, codigo, cupo, vence)"
                          " VALUES (%s, %s, %s, now() + %s * interval '1 day') RETURNING id",
                          nombre, secrets.token_urlsafe(12), cupo, dias).fetchone()["id"]
                anotar(f"Creó el grupo de estudiantes {nombre} (cupo {cupo}, registro abierto {dias} días)")
                return redirect(url_for("grupo", gid=gid))
        elif accion == "clave":
            if len(clave) < CLAVE_MINIMA:
                flash(f"La contraseña debe tener al menos {CLAVE_MINIMA} caracteres.", "error")
            elif sql("UPDATE cuentas SET clave = %s, fallos = 0, bloqueada_hasta = NULL WHERE cif = %s RETURNING cif",
                     generate_password_hash(clave), cif).fetchone():
                anotar(f"Le puso una contraseña nueva a {nombre_de(cif)} ({cif})")
                flash(f"Contraseña de {nombre_de(cif)} cambiada; si estaba bloqueada, ya puede entrar.", "ok")
        elif accion == "editar":
            nombre, rol_nuevo = request.form.get("nombre", "").strip(), request.form.get("rol")
            if not nombre or rol_nuevo not in ROLES:
                flash("Escriba el nombre y elija el rol.", "error")
            elif cif == yo and rol_nuevo != rol:
                flash("No puede cambiar el rol de su propia cuenta.", "error")
            elif fila := sql("UPDATE cuentas SET nombre = %s, rol = %s WHERE cif = %s RETURNING cif",
                             nombre, rol_nuevo, cif).fetchone():
                anotar(f"Corrigió la cuenta {fila['cif']}: {nombre}, {ROLES[rol_nuevo]}")
                flash(f"Cuenta de {nombre} guardada.", "ok")
        elif accion == "activa":
            if cif == yo:
                flash("No puede desactivar su propia cuenta.", "error")
            elif fila := sql("UPDATE cuentas SET activa = NOT activa WHERE cif = %s RETURNING nombre, activa",
                             cif).fetchone():
                anotar(("Activó" if fila["activa"] else "Desactivó") + f" la cuenta de {fila['nombre']} ({cif})")
                flash(f"Cuenta de {fila['nombre']} " + ("activada." if fila["activa"] else "desactivada."), "ok")
        return redirect(url_for("administrar_cuentas", **request.args))
    filtro = {"rol": request.args.get("rol", ""), "estado": request.args.get("estado", "")}
    return render_template("cuentas.html", **comun(), roles=ROLES, clave_minima=CLAVE_MINIMA, filtro=filtro,
                           anios=ANIOS, nombre_de=nombre_de,
                           cupo_maximo=CUPO_MAXIMO, dias_maximo=DIAS_MAXIMO,
                           grupos=sql(INVITACIONES + " ORDER BY i.id DESC").fetchall(), lista=sql(
        "SELECT cif, nombre, rol, activa, correo, anio, profesor, CASE WHEN bloqueada_hasta > now() THEN"
        " to_char(bloqueada_hasta AT TIME ZONE 'America/Managua', 'HH24:MI') END AS bloqueada"
        " FROM cuentas WHERE (%s = '' OR rol = %s) AND (%s = '' OR activa = (%s = 'activas'))"
        " ORDER BY activa DESC, rol, nombre", filtro["rol"], filtro["rol"], filtro["estado"], filtro["estado"]).fetchall())


@app.route("/cuentas/<cif>", methods=["GET", "POST"])
def cuenta(cif):
    """Ficha de una cuenta para la Facultad: nombre, rol, correo y año (RF02); el profesor a cargo del estudiante
    (RF06) y sus turnos clínicos (RF10)."""
    yo, _, rol = quien()
    if rol != "admin":
        return redirect(url_for("menu"))
    c = sql("SELECT * FROM cuentas WHERE cif = %s", cif).fetchone()
    if not c:
        return redirect(url_for("administrar_cuentas"))
    profesores = {p["cif"]: p["nombre"] for p in sql(
        "SELECT cif, nombre FROM cuentas WHERE rol = 'profesor' AND activa ORDER BY nombre").fetchall()}
    if request.method == "POST":
        accion = request.form.get("accion")
        if accion == "datos":
            f = {k: request.form.get(k, "").strip() for k in ("nombre", "rol", "correo", "profesor")}
            anio = request.form.get("anio", type=int)
            if not f["nombre"] or len(f["nombre"]) > NOMBRE_MAXIMO or f["rol"] not in ROLES:
                flash("Escriba el nombre y elija el rol.", "error")
            elif cif == yo and f["rol"] != rol:
                flash("No puede cambiar el rol de su propia cuenta.", "error")
            elif f["correo"] and not CORREO.fullmatch(f["correo"]):
                flash("Ese correo no es válido.", "error")
            elif f["profesor"] and f["profesor"] not in profesores:
                flash("Elija un profesor de la lista.", "error")
            else:
                estudiante = f["rol"] == "estudiante"
                sql("UPDATE cuentas SET nombre = %s, rol = %s, correo = %s, anio = %s, profesor = %s WHERE cif = %s",
                    f["nombre"], f["rol"], f["correo"], anio if estudiante and anio in ANIOS else None,
                    (f["profesor"] or None) if estudiante else None, cif)
                if not estudiante:
                    sql("DELETE FROM turnos WHERE estudiante = %s", cif)
                anotar(f"Corrigió la cuenta {cif}: {f['nombre']}, {ROLES[f['rol']]}"
                       + (f", profesor {profesores[f['profesor']]}" if estudiante and f["profesor"] else ""))
                flash(f"Cuenta de {f['nombre']} guardada.", "ok")
        elif accion == "turno" and c["rol"] == "estudiante":
            dia = request.form.get("dia", type=int)
            desde, hasta = request.form.get("desde", ""), request.form.get("hasta", "")
            horas = all(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", h) for h in (desde, hasta))
            if dia not in range(len(DIAS)) or not horas or desde >= hasta:
                flash("Elija el día y las horas del turno; la hora de inicio va antes de la de fin.", "error")
            elif sql("SELECT 1 FROM turnos WHERE estudiante = %s AND dia = %s AND desde < %s AND %s < hasta",
                     cif, dia, hasta, desde).fetchone():
                flash("Ese turno se cruza con otro que ya tiene ese día.", "error")
            else:
                sql("INSERT INTO turnos (estudiante, dia, desde, hasta) VALUES (%s, %s, %s, %s)", cif, dia, desde, hasta)
                anotar(f"Asignó a {c['nombre']} el turno del {DIAS[dia].lower()} de {desde} a {hasta}")
                flash("Turno agregado.", "ok")
        elif accion == "quitar_turno":
            t = sql("DELETE FROM turnos WHERE id = %s AND estudiante = %s RETURNING dia, desde, hasta",
                    request.form.get("id", type=int), cif).fetchone()
            if t:
                anotar(f"Quitó a {c['nombre']} el turno del {DIAS[t['dia']].lower()} de {t['desde']} a {t['hasta']}")
                flash("Turno quitado.", "ok")
        return redirect(url_for("cuenta", cif=cif))
    return render_template("cuenta.html", **comun(), c=c, roles=ROLES, anios=ANIOS, dias=DIAS, profesores=profesores,
                           turnos=turnos_de(cif), nombre_maximo=NOMBRE_MAXIMO, grupo=c["invitacion_id"] and sql(
                               "SELECT nombre FROM invitaciones WHERE id = %s", c["invitacion_id"]).fetchone(),
                           estudiantes=sql("SELECT cif, nombre FROM cuentas WHERE profesor = %s ORDER BY nombre",
                                           cif).fetchall() if c["rol"] == "profesor" else [])


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
    if request.method == "POST" and request.form.get("accion") == "profesor":
        # RF06: el profesor a cargo de todos los que se registraron con este grupo
        profesor = request.form.get("profesor", "")
        fila = sql("SELECT nombre FROM cuentas WHERE cif = %s AND rol = 'profesor'", profesor).fetchone()
        if fila:
            n = sql("UPDATE cuentas SET profesor = %s WHERE invitacion_id = %s AND rol = 'estudiante'",
                    profesor, gid).rowcount
            grupo_nombre = sql("SELECT nombre FROM invitaciones WHERE id = %s", gid).fetchone()["nombre"]
            anotar(f"Puso a {fila['nombre']} a cargo de los {n} estudiantes del grupo {grupo_nombre}")
            flash(f"{fila['nombre']} queda a cargo de los {n} estudiantes del grupo.", "ok")
        else:
            flash("Elija el profesor.", "error")
        return redirect(url_for("grupo", gid=gid))
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
        registrados=sql("SELECT cif, nombre, activa, correo, anio, profesor FROM cuentas WHERE invitacion_id = %s"
                        " ORDER BY nombre", gid).fetchall(), nombre_de=nombre_de,
        profesores=sql("SELECT cif, nombre FROM cuentas WHERE rol = 'profesor' AND activa ORDER BY nombre").fetchall())


@app.route("/registro/<codigo>", methods=["GET", "POST"])
def registro(codigo):
    """Página a la que lleva el QR de un grupo: el estudiante escribe su CIF y su nombre y crea su contraseña.
    No pide sesión; la protege el código del grupo, que vence, tiene cupo y la Facultad puede cerrar.
    Solo crea cuentas de estudiante, y un estudiante nuevo no ve a ningún paciente."""
    invitacion = sql(INVITACIONES + " WHERE i.codigo = %s", codigo).fetchone()
    datos = {"cif": "", "nombre": "", "correo": "", "anio": None, "errores": {}}
    if request.method == "POST" and invitacion and invitacion["estado"] == "Abierto":
        cif, nombre, correo = (request.form.get(c, "").strip() for c in ("cif", "nombre", "correo"))
        anio = request.form.get("anio", type=int)
        clave, errores = request.form.get("clave", ""), datos["errores"]
        datos.update(cif=cif, nombre=nombre, correo=correo, anio=anio)
        ya_existe = "Ya existe una cuenta con ese CIF. Si es suya, entre con su contraseña; si no, avise a la Facultad."
        if not cif.isalnum() or len(cif) > CIF_MAXIMO:
            errores["cif"] = "Escriba su CIF, solo con números y letras."
        elif cif in cuentas():
            errores["cif"] = ya_existe
        if not nombre or len(nombre) > NOMBRE_MAXIMO:
            errores["nombre"] = "Escriba su nombre completo."
        if not CORREO.fullmatch(correo) or len(correo) > 120:
            errores["correo"] = "Escriba su correo, por ejemplo nombre@uamv.edu.ni."
        if anio not in ANIOS:
            errores["anio"] = "Elija el año de la carrera que cursa."
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
                    "INSERT INTO cuentas (cif, nombre, rol, clave, invitacion_id, correo, anio)"
                    " VALUES (%s, %s, 'estudiante', %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING cif",
                    cif, nombre, cifrada, invitacion["id"], correo, anio).fetchone()
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
    return render_template("registro.html", **datos, grupo=invitacion, clave_minima=CLAVE_MINIMA, anios=ANIOS,
                           cif_maximo=CIF_MAXIMO, nombre_maximo=NOMBRE_MAXIMO)


def recibos_de(estudiante=None, paciente_id=None):
    """Los recibos de la hoja de control con su paciente y tratamiento; de un estudiante o de un paciente."""
    return sql("SELECT r.id, r.numero, r.fecha, r.estudiante, r.firmado, r.firmado_por, p.id AS pid,"
               " p.nombre AS paciente, p.expediente, a.nombre || coalesce(' del ' || t.diente, '') AS tratamiento"
               " FROM recibos r JOIN tratamientos t ON t.id = r.tratamiento_id"
               " JOIN pacientes p ON p.id = t.paciente_id JOIN tarifario a ON a.id = t.procedimiento_id"
               " WHERE p.eliminado IS NULL AND (%s::text IS NULL OR r.estudiante = %s) AND (%s::int IS NULL OR p.id = %s)"
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
                flash("Suba la foto del recibo: JPG o PNG de hasta 5 MB, o tómela con la cámara.", "error")
            elif sql("SELECT 1 FROM recibos WHERE tratamiento_id = %s AND numero = %s", t["id"], numero).fetchone():
                flash("Ese recibo ya está anotado para ese tratamiento.", "error")
            else:
                url = guardar_archivo("recibos", imagen)
                if sql("INSERT INTO recibos (tratamiento_id, numero, fecha, url, estudiante) VALUES (%s, %s, %s, %s, %s)"
                       " ON CONFLICT DO NOTHING RETURNING id", t["id"], numero, fecha, url, cif).fetchone():
                    anotar(f"Subió el recibo {numero} de {t['paciente']}: {t['nombre']}")
                    flash(f"Recibo {numero} agregado a la hoja de control.", "ok")
                else:  # otro lo anoto en el mismo instante
                    borrar_archivos([url])
                    flash("Ese recibo ya está anotado para ese tratamiento.", "error")
        elif rol == "estudiante" and accion == "quitar":
            # Lo firmado por el profesor ya no se quita
            r = sql("DELETE FROM recibos WHERE id = %s AND estudiante = %s AND firmado IS NULL RETURNING numero, url",
                    request.form.get("id", type=int), cif).fetchone()
            if r:
                borrar_archivos([r["url"]])
                anotar(f"Quitó el recibo {r['numero']} de su hoja de control")
                flash(f"Recibo {r['numero']} quitado.", "ok")
        elif rol == "profesor" and accion in ("firmar", "quitar_firma"):
            if accion == "firmar":  # solo los recibos de sus estudiantes
                r = sql("UPDATE recibos SET firmado_por = %s, firmado = %s WHERE id = %s AND firmado IS NULL AND "
                        + de_mi_grupo("recibos.estudiante") + " RETURNING numero, estudiante",
                        cif, hoy(), request.form.get("id", type=int), cif).fetchone()
            else:  # solo el profesor que firmó puede quitar su firma
                r = sql("UPDATE recibos SET firmado_por = NULL, firmado = NULL WHERE id = %s AND firmado_por = %s"
                        " RETURNING numero, estudiante", request.form.get("id", type=int), cif).fetchone()
            if r:
                anotar(("Firmó" if accion == "firmar" else "Quitó su firma de")
                       + f" el recibo {r['numero']} de {nombre_de(r['estudiante'])}")
                flash(f"Recibo {r['numero']} " + ("firmado." if accion == "firmar" else "sin firma otra vez."), "ok")
        return redirect(url_for("control", estudiante=request.args.get("estudiante") or None))
    estudiantes = [(e["cif"], e["nombre"]) for e in sql(
        "SELECT cif, nombre FROM cuentas WHERE rol = 'estudiante' AND (%s OR " + de_mi_grupo("cuentas.cif")
        + ") ORDER BY nombre", rol != "profesor", cif).fetchall()]
    elegido = cif if rol == "estudiante" else request.args.get("estudiante") or None
    if elegido not in dict(estudiantes):
        elegido = None
    pendientes = []  # los tratamientos que él realizó, para elegir a cuál corresponde el recibo
    if rol == "estudiante":
        pendientes = sql("SELECT t.id, p.nombre AS paciente, a.nombre || coalesce(' del ' || t.diente, '') AS tratamiento"
                         " FROM tratamientos t JOIN pacientes p ON p.id = t.paciente_id"
                         " JOIN tarifario a ON a.id = t.procedimiento_id"
                         " WHERE t.estudiante = %s AND t.realizado IS NOT NULL AND p.eliminado IS NULL"
                         " ORDER BY p.nombre, t.id", cif).fetchall()
    recibos = recibos_de(estudiante=elegido)
    if rol == "profesor":  # sin elegir estudiante, los recibos de todos los de su grupo
        recibos = [r for r in recibos if r["estudiante"] in dict(estudiantes)]
    return render_template("control.html", **comun(), recibos=recibos, elegido=elegido,
                           estudiantes=estudiantes, pendientes=pendientes, hoy=hoy(), nombre_de=nombre_de,
                           recibo_maximo=RECIBO_MAXIMO)


@app.route("/control/recibos/<int:rid>.jpg")
def recibo_foto(rid):
    # El estudiante ve solo sus recibos; el profesor y la Facultad, todos
    cif, _, rol = quien()
    r = sql("SELECT url FROM recibos WHERE id = %s AND CASE %s WHEN 'estudiante' THEN estudiante = %s"
            " WHEN 'profesor' THEN " + de_mi_grupo("recibos.estudiante") + " ELSE true END",
            rid, rol, cif, cif).fetchone()
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
    lineas = sql("SELECT to_char(cuando AT TIME ZONE 'America/Managua', 'DD/MM/YYYY HH24:MI') AS cuando,"
                 " cif, nombre, rol, accion, lugar FROM bitacora"
                 " WHERE (%s = '' OR (cuando AT TIME ZONE 'America/Managua')::date >= nullif(%s, '')::date)"
                 " AND (%s = '' OR (cuando AT TIME ZONE 'America/Managua')::date <= nullif(%s, '')::date)"
                 " AND (%s = '' OR nombre ILIKE %s OR cif ILIKE %s) AND (%s = '' OR accion ILIKE %s)"
                 " ORDER BY id DESC LIMIT 500", f["desde"], f["desde"], f["hasta"], f["hasta"],
                 f["usuario"], como(f["usuario"]), como(f["usuario"]), f["accion"], como(f["accion"])).fetchall()
    return render_template("bitacora.html", **comun(), lineas=lineas, f=f, filtrado=any(f.values()))


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
                            ("Estado", "Aprobado" if p["aprobado"] else f"Rechazado: {p['rechazo']}" if p["rechazo"]
                             else "Pendiente de aprobación")):
        linea(f"{etiqueta}: {valor}")
    if p["finalizado"]:
        linea(f"Recibió el alta el {dmy(p['finalizado'])} por {nombre_de(p['finalizado_por'])}: "
              f"{p['finalizado_resumen']}")

    y -= 10
    linea("Historia clínica", "Helvetica-Bold", 12)
    h = historia_de(pid)
    for c, etiqueta in HISTORIA.items():
        if h.get(c):
            linea(f"{etiqueta}: {dmy(h[c]) if c == 'nacimiento' else h[c]}"[:110], "Helvetica", 10)
    for a in alertas_de(h):
        linea(f"ALERTA - {a}"[:110], "Helvetica-Bold", 10)

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
            linea(f"- {dmy(c['fecha'])} {c['hora']} · {c['razon']} · {c['sala'] or 'sin clínica'} · {c['estado']}"
                  + (f" ({c['motivo_cancelacion']})" if c["motivo_cancelacion"] else ""), "Helvetica", 10)

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
        linea(f"- {t['fase']}: {t['nombre']}" + (f" del {t['diente']}" if t["diente"] else "") + f" · {cordobas(t['precio'])} · "
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
