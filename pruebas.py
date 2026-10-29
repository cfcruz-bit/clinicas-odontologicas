"""Pruebas de Clínicas Odontológicas. Borran todo lo que haya en la base, así que usan su propia rama de Neon.

Ejecutar:  PRUEBAS_DATABASE_URL=<cadena de la rama de pruebas> python pruebas.py
"""
import os
import sys

import psycopg
from psycopg.rows import dict_row

# Nunca se toma DATABASE_URL: las pruebas no deben tocar la base de verdad
os.environ["DATABASE_URL"] = os.environ["PRUEBAS_DATABASE_URL"]
import prototipo as m  # noqa: E402

base = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row, prepare_threshold=None)
base.execute("DROP TABLE IF EXISTS bitacora, citas, trabajo, pacientes, salas, cuentas")
m.crear_tablas()

def uno(consulta, *args):
    return base.execute(consulta, args).fetchone()

def cuantos(tabla):
    return uno(f"SELECT count(*) AS n FROM {tabla}")["n"]

fallos = []

def caso(descripcion, condicion):
    print(("  OK     " if condicion else "  FALLO  ") + descripcion)
    if not condicion:
        fallos.append(descripcion)

def cliente(cif):
    c = m.app.test_client()
    c.post("/", data={"accion": "entrar", "cif": cif, "clave": "uam2026"})
    return c

def texto(r):
    return r.get_data(as_text=True)

grace, gabriel, profe, admin = cliente("00000002"), cliente("00000005"), cliente("00000001"), cliente("00000007")

print("Clínicas Odontológicas - pruebas\n")
caso("Sin sesion la pagina de pacientes manda a la entrada",
     m.app.test_client().get("/pacientes").headers["Location"].endswith("/"))
caso("La clave se guarda cifrada, no en texto plano", uno("SELECT clave FROM cuentas LIMIT 1")["clave"] != "uam2026")
caso("Con una clave equivocada no se entra",
     m.app.test_client().post("/", data={"accion": "entrar", "cif": "00000002", "clave": "otra"}).status_code == 200)
caso("El nombre del usuario sale en la cabecera, no solo el rol", "Grace Alvarado" in texto(grace.get("/menu")))

entrada = m.app.test_client().post("/", data={"accion": "entrar", "cif": "00000002", "clave": "uam2026"})
caso("La sesion se cierra sola a los 30 minutos sin uso",
     m.app.permanent_session_lifetime.total_seconds() == 1800 and "Expires=" in entrada.headers["Set-Cookie"])

rivas = cliente("00000004")
caso("Con la clave inicial se entra directo: no obliga a cambiarla", rivas.get("/menu").status_code == 200)
clave_de_rivas = lambda: uno("SELECT clave FROM cuentas WHERE cif = '00000004'")["clave"]  # noqa: E731
antes = clave_de_rivas()
rivas.post("/clave", data={"actual": "otra", "nueva": "nueva-clave-1", "repetir": "nueva-clave-1"})
rivas.post("/clave", data={"actual": "uam2026", "nueva": "corta", "repetir": "corta"})
rivas.post("/clave", data={"actual": "uam2026", "nueva": "nueva-clave-1", "repetir": "distinta-12"})
caso("No se cambia sin la clave actual, con una corta o si no coinciden", clave_de_rivas() == antes)
rivas.post("/clave", data={"actual": "uam2026", "nueva": "nueva-clave-1", "repetir": "nueva-clave-1"})
viejo = m.app.test_client()
viejo.post("/", data={"accion": "entrar", "cif": "00000004", "clave": "uam2026"})
nuevo = m.app.test_client()
nuevo.post("/", data={"accion": "entrar", "cif": "00000004", "clave": "nueva-clave-1"})
caso("Al cambiarla, la clave vieja deja de servir y la nueva entra",
     viejo.get("/menu").status_code == 302 and nuevo.get("/menu").status_code == 200)

grace.post("/pacientes", data={"accion": "registrar", "nombre": "Luis Pérez", "cedula": "T-100", "telefono": "88880000"})
pid = uno("SELECT id FROM pacientes")["id"]
paciente = lambda: uno("SELECT * FROM pacientes WHERE id = %s", pid)  # noqa: E731
caso("Al registrar, el paso siguiente pide la razon del paciente",
     "por qué está el paciente" in texto(grace.get(f"/pacientes/{pid}/razon")))
grace.post(f"/pacientes/{pid}/razon", data={"razon": ""})
caso("La razon es obligatoria", paciente()["razon"] == "")
grace.post(f"/pacientes/{pid}/razon", data={"razon": "Dolor en la muela 36", "fecha": m.dmy(m.hoy()),
                                            "hora": "09:00", "sala": "Sala 1", "razon_cita": "Valoración"})
caso("La razon sale al lado del nombre en la lista", "Dolor en la muela 36" in texto(grace.get("/pacientes")))
caso("La razon de la cita y la del paciente salen en la pagina de citas",
     all(t in texto(grace.get("/citas")) for t in ("Valoración", "Dolor en la muela 36")))

caso("Las fechas se escriben y se ven como dd/mm/aaaa",
     m.desde_dmy("28/10/2026") == "2026-10-28"
     and m.dmy(m.hoy()) in texto(grace.get("/citas")))
grace.post("/citas", data={"paciente_id": pid, "fecha": "2026-11-30", "hora": "10:00", "razon": "Control"})
caso("Una fecha en otro formato no se agenda", cuantos("citas") == 1)
grace.post("/citas", data={"paciente_id": pid, "fecha": m.dmy(m.hoy()), "hora": "09:00", "sala": "Sala 1",
                           "razon": "Otra"})
caso("No se agenda una sala ocupada a la misma hora", cuantos("citas") == 1)

grace.post("/citas", data={"paciente_id": pid, "fecha": m.dmy(m.hoy()), "hora": "11:00", "razon": "Para cancelar"})
cancelar = uno("SELECT id FROM citas WHERE razon = 'Para cancelar'")["id"]
gabriel.post("/citas", data={"accion": "cancelar", "id": cancelar})
caso("Un estudiante no cancela citas de pacientes ajenos", cuantos("citas") == 2)
grace.post("/citas", data={"accion": "cancelar", "id": cancelar})
caso("El estudiante a cargo cancela una cita", cuantos("citas") == 1)
base.execute("INSERT INTO citas (paciente_id, fecha, hora, razon) VALUES (%s, '2020-01-01', '08:00', 'Pasada')", (pid,))
pasada = uno("SELECT id FROM citas WHERE razon = 'Pasada'")["id"]
grace.post("/citas", data={"accion": "cancelar", "id": pasada})
caso("Las citas pasadas no se cancelan: son historial", cuantos("citas") == 2)

gabriel.post("/pacientes", data={"accion": "registrar", "nombre": "Ana Ruiz", "cedula": "T-200"})
grace.post(f"/pacientes/{pid}/editar", data={"nombre": "Luis Pérez", "cedula": "T-200", "telefono": ""})
caso("Al corregir no se puede usar la cedula de otro paciente", paciente()["cedula"] == "T-100")
gabriel.post(f"/pacientes/{pid}/editar", data={"nombre": "Otro", "cedula": "T-100", "telefono": ""})
caso("Un estudiante no corrige pacientes ajenos", paciente()["nombre"] == "Luis Pérez")
grace.post(f"/pacientes/{pid}/editar", data={"nombre": "Luis Pérez", "cedula": "T-100", "telefono": "22220000"})
caso("El estudiante a cargo corrige los datos del paciente", paciente()["telefono"] == "22220000")

caso("Cada estudiante ve solo sus pacientes", "Luis Pérez" not in texto(gabriel.get("/pacientes")))
caso("Cada estudiante ve solo sus citas", "Luis Pérez" not in texto(gabriel.get("/citas")))
caso("Un estudiante no abre el expediente de otro", gabriel.get(f"/pacientes/{pid}").status_code == 302)
caso("El profesor ve a todos los pacientes", "Luis Pérez" in texto(profe.get("/pacientes")))

caso("El profesor no registra pacientes", "accion\" value=\"registrar" not in texto(profe.get("/pacientes")))
profe.post("/pacientes", data={"accion": "registrar", "nombre": "X", "cedula": "T-999"})
caso("El profesor no puede registrar ni por la fuerza", cuantos("pacientes") == 2)
caso("El profesor no agenda citas", "Agendar cita" not in texto(profe.get("/citas")))
profe.post("/pacientes", data={"accion": "aprobar", "id": pid, "aprobado": "1"})
caso("El profesor solo revisa: aprueba al paciente", paciente()["aprobado"] is True)

FIRMA = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAgAAAAECAYAAACzzX7wAAAA"
         "FUlEQVR4nGNkYGD4z4AHMOGTpI4CAIIUAQeqcFo1AAAAAElFTkSuQmCC")
grace.post(f"/pacientes/{pid}/consentimiento", data={"firma": "Luis Pérez"})
caso("La firma escrita a mano no cuenta: el paciente la dibuja", paciente()["firma"] == "")
grace.post(f"/pacientes/{pid}/consentimiento", data={"firma": FIRMA})
hoja = texto(grace.get(f"/pacientes/{pid}/consentimiento"))
caso("El consentimiento se imprime con la firma dibujada del paciente",
     paciente()["firma"] == FIRMA and "firma-trazo" in hoja)
caso("El docente no firma el consentimiento", "Firma del docente" not in hoja)
caso("La lista dice que ya firmo", "Firmado" in texto(grace.get("/pacientes")))

pdf = grace.get(f"/pacientes/{pid}/expediente.pdf")
caso("El expediente se descarga como PDF, no como txt",
     pdf.data[:4] == b"%PDF" and pdf.headers["Content-Disposition"].endswith(".pdf"))

caso("Solo la administracion traspasa pacientes",
     grace.get(f"/pacientes/{pid}/traspaso").status_code == 302)
admin.post(f"/pacientes/{pid}/traspaso", data={"estudiante": "00000005", "resumen": ""})
caso("Al traspasar hay que decir que se le hizo al paciente", paciente()["estudiante"] == "00000002")
admin.post(f"/pacientes/{pid}/traspaso", data={"estudiante": "00000005", "resumen": "Profilaxis y obturación del 36"})
archivado = base.execute("SELECT * FROM trabajo WHERE paciente_id = %s AND hasta IS NOT NULL", (pid,)).fetchall()
caso("Al cambiar de estudiante se archiva el trabajo del anterior con su resumen",
     paciente()["estudiante"] == "00000005" and len(archivado) == 1
     and archivado[0]["resumen"] == "Profilaxis y obturación del 36")
caso("El historial muestra que le hizo cada estudiante",
     all(t in texto(gabriel.get(f"/pacientes/{pid}")) for t in ("Grace Alvarado", "Gabriel Espinoza",
                                                                "Profilaxis y obturación del 36")))
caso("El paciente ya no le sale al estudiante anterior", "Luis Pérez" not in texto(grace.get("/pacientes")))

plano = texto(admin.get("/salas")).split('class="plano-salas"')[1].split("plano-pasillo")[0]
caso("El plano marca las salas disponibles con un check y las ocupadas con una X",
     plano.count("✓") == 2 and plano.count("✗") == 1)
sala1 = uno("SELECT id FROM salas WHERE nombre = 'Sala 1'")["id"]
admin.post("/salas", data={"sala": sala1})
caso("La administracion cambia la disponibilidad de una sala",
     uno("SELECT disponible FROM salas WHERE id = %s", sala1)["disponible"] is False)
caso("El estudiante no administra las salas", "Agregar sala" not in texto(grace.get("/salas")))
caso("La bitacora guarda lo que hizo cada quien", "Pasó a Luis Pérez" in texto(admin.get("/bitacora")))

m.crear_tablas()
caso("Crear las tablas otra vez no borra ni duplica nada", cuantos("pacientes") == 2 and cuantos("cuentas") == 5)

print(f"\n{'Todo correcto.' if not fallos else str(len(fallos)) + ' fallos.'}")
sys.exit(1 if fallos else 0)
