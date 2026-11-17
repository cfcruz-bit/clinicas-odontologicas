"""Prueba de carga (RNF02): 50 usuarios a la vez (40 estudiantes y 10 profesores) usando el sistema durante
unos minutos. Aprobado si el 95 % de las paginas responde en menos de 3 s y no hay errores.

Usa solo una rama de pruebas de Neon, porque la VACIA y la llena con cuentas y pacientes ficticios.
    CARGA_DATABASE_URL=<rama de pruebas> python carga.py [segundos]
Levanta el sistema en este mismo equipo (gunicorn, como en Fly) y le manda las visitas. Para medir mejor, el
sistema va en otra maquina y CARGA_SITIO=http://<su direccion>:8080 (con esa misma base y sin FLY_APP_NAME).
"""
import os
import random
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

URL_BD = os.environ.get("CARGA_DATABASE_URL", "")
if not URL_BD:
    sys.exit("Falta CARGA_DATABASE_URL con la cadena de una rama de pruebas de Neon (nunca la base real).")
DURACION = int(sys.argv[1]) if len(sys.argv) > 1 else 300
SITIO = os.environ.get("CARGA_SITIO", "http://127.0.0.1:8090")
CLAVE = "carga2026"

# SECRET_KEY fija: con una distinta en cada proceso de gunicorn la sesion se perderia entre paginas
entorno = {"SECRET_KEY": "carga", **os.environ, "DATABASE_URL": URL_BD, "ALMACEN_BUCKET": "", "CLAVE_INICIAL": CLAVE}
entorno.pop("FLY_APP_NAME", None)  # sin esto la cookie de sesion pide HTTPS y aqui se entra por http

import psycopg  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

print("Preparando la base de pruebas...")
with psycopg.connect(URL_BD, autocommit=True) as bd:
    bd.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
subprocess.run([sys.executable, "-m", "flask", "--app", "prototipo", "crear-tablas"], env=entorno, check=True)
clave = generate_password_hash(CLAVE)
profesores = [f"9{n:07d}" for n in range(10)]
estudiantes = [f"8{n:07d}" for n in range(40)]
with psycopg.connect(URL_BD, autocommit=True) as bd:
    for i, cif in enumerate(profesores):
        bd.execute("INSERT INTO cuentas (cif, nombre, rol, clave) VALUES (%s, %s, 'profesor', %s)",
                   (cif, f"Profesor {i}", clave))
    for i, cif in enumerate(estudiantes):
        bd.execute("INSERT INTO cuentas (cif, nombre, rol, clave, profesor) VALUES (%s, %s, 'estudiante', %s, %s)",
                   (cif, f"Estudiante {i}", clave, profesores[i % 10]))
        for j in range(5):  # 200 pacientes en total
            bd.execute("INSERT INTO pacientes (nombre, cedula, estudiante, aprobado, expediente)"
                       " VALUES (%s, %s, %s, true, %s)",
                       (f"Paciente {i}-{j}", f"001-0101{i:02d}-00{j:02d}A", cif, f"EXP-{i:03d}{j}"))
    mios = {cif: [r[0] for r in bd.execute("SELECT id FROM pacientes WHERE estudiante = %s", (cif,))]
            for cif in estudiantes}

servidor = None if "CARGA_SITIO" in os.environ else subprocess.Popen(
    ["gunicorn", "--bind", "127.0.0.1:8090", "--workers", "2", "--threads", "4", "prototipo:app"], env=entorno)
for _ in range(60):
    try:
        urllib.request.urlopen(SITIO + "/")
        break
    except OSError:
        time.sleep(0.5)

tiempos = {}  # pagina -> [segundos]
errores = []
candado = threading.Lock()

def visitar(abrir, nombre, ruta, datos=None):
    inicio = time.perf_counter()
    try:
        r = abrir.open(SITIO + ruta, urllib.parse.urlencode(datos).encode() if datos else None, timeout=30)
        r.read()
        # Si la sesion se perdio, el sistema manda a la pagina de entrada: eso es un error, no una visita rapida
        ok = r.status == 200 and urllib.parse.urlparse(r.geturl()).path == ("/menu" if nombre == "Entrar" else ruta)
        if not ok:
            r = f"terminó en {r.geturl()}"
    except Exception as e:  # noqa: BLE001
        ok, r = False, e
    with candado:
        tiempos.setdefault(nombre, []).append(time.perf_counter() - inicio)
        if not ok:
            errores.append(f"{nombre}: {r}")

def usuario(cif, fin):
    abrir = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    visitar(abrir, "Entrar", "/", {"cif": cif, "clave": CLAVE})
    while time.time() < fin:
        if cif in mios:
            pid = random.choice(mios[cif])
            paso = random.choice(["Inicio", "Pacientes", "Expediente", "Historia", "Guardar historia",
                                  "Citas", "Odontograma", "Tratamientos", "Clínicas"])
            ruta = {"Inicio": "/menu", "Pacientes": "/pacientes", "Expediente": f"/pacientes/{pid}",
                    "Historia": f"/pacientes/{pid}/historia", "Guardar historia": f"/pacientes/{pid}/historia",
                    "Citas": "/citas", "Odontograma": f"/pacientes/{pid}/odontograma",
                    "Tratamientos": f"/pacientes/{pid}/tratamientos", "Clínicas": "/salas"}[paso]
            datos = {"diagnostico": f"Revisión {random.randint(1, 999)}", "accion": "borrador"} \
                if paso == "Guardar historia" else None
        else:
            paso = random.choice(["Inicio", "Pacientes", "Citas", "Hoja de control", "Clínicas"])
            ruta = {"Inicio": "/menu", "Pacientes": "/pacientes", "Citas": "/citas", "Hoja de control": "/control",
                    "Clínicas": "/salas"}[paso]
            datos = None
        visitar(abrir, paso, ruta, datos)
        time.sleep(random.uniform(1, 3))  # lo que tarda una persona en leer y hacer clic

print(f"50 usuarios durante {DURACION} s...")
fin = time.time() + DURACION
hilos = [threading.Thread(target=usuario, args=(cif, fin)) for cif in estudiantes + profesores]
for h in hilos:
    h.start()
    time.sleep(0.2)  # van entrando de a poco, como al empezar un turno
for h in hilos:
    h.join()
if servidor:
    servidor.terminate()

def p95(xs):
    return sorted(xs)[max(0, int(len(xs) * 0.95) - 1)]

todos = [t for xs in tiempos.values() for t in xs]
print(f"\n{'Página':<18}{'Visitas':>8}{'Promedio':>10}{'95 %':>8}{'Máximo':>8}")
for nombre, xs in sorted(tiempos.items()):
    print(f"{nombre:<18}{len(xs):>8}{statistics.mean(xs):>9.2f}s{p95(xs):>7.2f}s{max(xs):>7.2f}s")
print(f"{'TOTAL':<18}{len(todos):>8}{statistics.mean(todos):>9.2f}s{p95(todos):>7.2f}s{max(todos):>7.2f}s")
print(f"Errores: {len(errores)}", *errores[:10], sep="\n  ")
aprobado = p95(todos) < 3 and not errores
print("\nRNF02:", "APROBADO" if aprobado else "NO APROBADO", "(meta: 95 % de las páginas en menos de 3 s, sin errores)")
sys.exit(0 if aprobado else 1)
