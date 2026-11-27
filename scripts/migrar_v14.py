"""Pasa una base del modelo de datos anterior al modelo v14 (esquema.sql). Se corre UNA vez, en una rama nueva de
Neon que es copia de la de producción; la base de producción no se toca.

    MIGRAR_DATABASE_URL=<cadena de la rama nueva> python scripts/migrar_v14.py

Las tablas viejas pasan al esquema "anterior" (no se borran) y se crean las del modelo v14 con los datos
convertidos. Al final imprime qué no se pudo pasar tal cual y por qué. Todo va en una sola transacción: si algo
falla, la base queda como estaba.
"""
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

CORREO_PENDIENTE = "@correo-pendiente.uam"  # el mismo de prototipo.py
VIEJAS = ("pedidos_contacto", "turnos", "historias", "precios_anteriores", "solicitudes", "ordenes", "recibos",
          "abonos", "tratamientos", "tarifario", "materiales", "fotos", "dientes", "reportes", "bitacora", "citas",
          "trabajo", "pacientes", "salas", "cuentas", "invitaciones")
# Del valor viejo al del modelo v14
MAPAS = {
    "rol": {"admin": "facultad", "profesor": "profesor", "estudiante": "estudiante"},
    "sexo": {"Femenino": "F", "Masculino": "M"},
    "diente": {"Sano": "sano", "Caries": "caries", "Obturado": "obturado", "Endodoncia": "endodoncia",
               "Corona": "corona", "Extracción indicada": "extraccion", "Extraído": "ausente"},
    "foto": {"Presencial": "extraoral", "Radiografía": "radiografia"},
    "cita": {"Programada": "programada", "Confirmada": "confirmada", "Atendida": "atendida",
             "Cancelada": "cancelada", "No se presentó": "no_asistio"},
    "falla": {"Necesita limpieza": "limpieza", "Necesita reparación": "equipo", "Fuera de servicio": "otro"},
    # Las fases viejas eran por especialidad; las del modelo son por etapa del plan
    "fase": {"Higiénica": "basica", "Periodoncia": "basica", "Cirugía": "basica", "Operatoria": "rehabilitacion",
             "Endodoncia": "rehabilitacion", "Prótesis": "rehabilitacion", "Mantenimiento": "mantenimiento"},
    "solicitud": {"Pendiente": "pendiente", "Entregada": "aprobada", "Rechazada": "rechazada"},
}
# Una fecha 'aaaa-mm-dd' de antes, como momento: al mediodia de Managua (asi no cambia de dia al mostrarla)
MEDIODIA = "(({0})::date + time '12:00') AT TIME ZONE 'America/Managua'"


def mapa(nombre, columna):
    """Expresion SQL CASE que traduce la columna con el mapa (NULL si el valor no esta)."""
    casos = " ".join(f"WHEN {columna} = '{a}' THEN '{b}'" for a, b in MAPAS[nombre].items())
    return f"CASE {casos} END"


def migrar(c):
    avisos = []
    q = lambda s, *a: c.execute(s, a or None)  # noqa: E731
    uno = lambda s, *a: c.execute(s, a or None).fetchone()  # noqa: E731
    if not uno("SELECT to_regclass('public.cuentas') AS t")["t"] or uno(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'cuentas'"
            " AND column_name = 'id'"):
        sys.exit("Esta base no tiene el modelo anterior (o ya se migró).")

    q("CREATE SCHEMA anterior")
    for t in VIEJAS:
        q(f"ALTER TABLE IF EXISTS public.{t} SET SCHEMA anterior")
    q(Path(__file__).resolve().parent.parent.joinpath("esquema.sql").read_text(encoding="utf-8"))
    # Son datos que ya pasaron las reglas del sistema anterior: las reglas automaticas de rol y de aprobacion no
    # se repiten (las CHECK y las de no encimar citas si se revisan)
    q("SET LOCAL clinicas.restaurando = 'si'")

    # --- Cuentas (el id es nuevo; el CIF sigue siendo unico) ---
    q(f"""INSERT INTO cuentas (cif, nombre, correo, anio, rol, clave_hash, activa, fallos, bloqueada_hasta)
          SELECT cif, nombre, CASE WHEN correo <> '' THEN correo ELSE cif || %s END,
                 CASE WHEN rol = 'estudiante' THEN coalesce(anio, 1) END, {mapa('rol', 'rol')}, clave, activa,
                 fallos, bloqueada_hasta FROM anterior.cuentas ORDER BY cif""", CORREO_PENDIENTE)
    sin_correo = [r["cif"] for r in q("SELECT cif FROM cuentas WHERE correo LIKE %s", "%" + CORREO_PENDIENTE)]
    if sin_correo:
        avisos.append(f"{len(sin_correo)} cuentas no tenían correo: quedaron con uno provisional (<CIF>{CORREO_PENDIENTE})"
                      f" que la Facultad debe corregir en la ficha: {', '.join(sin_correo)}")
    sin_anio = [r["cif"] for r in q("SELECT cif FROM anterior.cuentas WHERE rol = 'estudiante' AND anio IS NULL")]
    if sin_anio:
        avisos.append(f"{len(sin_anio)} estudiantes no tenían año de la carrera: quedaron en 1.º hasta que la Facultad"
                      f" lo corrija: {', '.join(sin_anio)}")
    ID = "(SELECT id FROM cuentas WHERE cif = {0})"  # cuenta nueva a partir del CIF viejo

    # --- Clases: el profesor que tenia asignado cada estudiante pasa a ser su clase ---
    for r in q("SELECT DISTINCT profesor FROM anterior.cuentas WHERE profesor IS NOT NULL AND rol = 'estudiante'").fetchall():
        k = uno(f"INSERT INTO clases (nombre, periodo, profesor_id) VALUES ('Estudiantes asignados', "
                f"to_char(now(), 'YYYY') || CASE WHEN extract(month FROM now()) <= 6 THEN '-I' ELSE '-II' END, {ID.format('%s')})"
                " RETURNING id", r["profesor"])
        q("UPDATE cuentas SET clase_id = %s WHERE cif IN (SELECT cif FROM anterior.cuentas WHERE profesor = %s"
          " AND rol = 'estudiante')", k["id"], r["profesor"])
    grupos = uno("SELECT count(*) AS n FROM anterior.invitaciones")["n"]
    if grupos:
        avisos.append(f"Los {grupos} grupos con QR de la Facultad no pasan: en el modelo nuevo cada profesor crea su"
                      " clase y sus códigos QR (de un solo uso).")

    # --- Turnos (dia 0-5 pasa a 1-6) ---
    q(f"""INSERT INTO turnos (estudiante_id, dia, desde, hasta)
          SELECT {ID.format('estudiante')}, dia + 1, desde::time, hasta::time FROM anterior.turnos
          WHERE desde >= '07:00' AND hasta <= '16:00'""")
    fuera = uno("SELECT count(*) AS n FROM anterior.turnos WHERE NOT (desde >= '07:00' AND hasta <= '16:00')")["n"]
    if fuera:
        avisos.append(f"{fuera} turnos fuera del horario de la clínica (07:00 a 16:00) no se pasaron.")

    # --- Bitácora (se conserva el CIF tal como quedó anotado) ---
    q(f"""INSERT INTO bitacora (cuenta_id, cif_registrado, accion, lugar, creado_en)
          SELECT {ID.format('b.cif')}, coalesce(b.cif, 'SISTEMA'), b.accion, b.lugar, b.cuando
          FROM anterior.bitacora b ORDER BY b.id""")

    # --- Clínicas (mismos números) ---
    q("INSERT INTO salas (id, nombre, habilitada, en_sala) SELECT id, nombre, disponible, en_sala FROM anterior.salas")

    # --- Pacientes (mismos números). La fecha de nacimiento y el sexo pasan de la historia al paciente ---
    q(f"""INSERT INTO pacientes (id, cedula, expediente, nombre, nacimiento, sexo, telefono, estudiante_id,
                                 aprobado_por_id, aprobado_en, creado_en, razon, firma, firmado_en, rechazo,
                                 rechazado_por_id, rechazado_en, odonto_revision, odonto_revisor_id, odonto_nota,
                                 finalizado_en, finalizado_por_id, finalizado_resumen, eliminado_en,
                                 eliminado_por_id, eliminado_motivo)
          SELECT p.id, p.cedula, CASE WHEN p.expediente <> '' THEN p.expediente ELSE 'EXP-' || lpad(p.id::text, 5, '0') END,
                 p.nombre, h.nacimiento::date, {mapa('sexo', 'h.sexo')}, p.telefono, {ID.format('p.estudiante')},
                 CASE WHEN p.aprobado AND p.decision_por IS NOT NULL THEN {ID.format('p.decision_por')} END,
                 CASE WHEN p.aprobado AND p.decision_por IS NOT NULL THEN coalesce(p.decision_cuando, now()) END,
                 coalesce((SELECT {MEDIODIA.format('min(t.desde)')} FROM anterior.trabajo t WHERE t.paciente_id = p.id), now()),
                 p.razon, p.firma, CASE WHEN p.firma_fecha IS NOT NULL THEN {MEDIODIA.format('p.firma_fecha')} END,
                 p.rechazo, CASE WHEN p.rechazo <> '' THEN {ID.format('p.decision_por')} END,
                 CASE WHEN p.rechazo <> '' THEN p.decision_cuando END,
                 p.odonto_revision, {ID.format('p.odonto_revisor')}, p.odonto_nota,
                 CASE WHEN p.finalizado IS NOT NULL THEN {MEDIODIA.format('p.finalizado')} END,
                 {ID.format('p.finalizado_por')}, p.finalizado_resumen, p.eliminado, {ID.format('p.eliminado_por')},
                 p.eliminado_motivo
          FROM anterior.pacientes p LEFT JOIN anterior.historias h ON h.paciente_id = p.id""")
    sin_quien = [r["nombre"] for r in q("SELECT nombre FROM anterior.pacientes WHERE aprobado AND decision_por IS NULL")]
    if sin_quien:
        avisos.append(f"{len(sin_quien)} pacientes estaban aprobados sin que quedara qué profesor los aprobó; el modelo"
                      f" lo exige, así que vuelven a esperar la aprobación: {', '.join(sin_quien)}")

    q("""INSERT INTO historias (paciente_id, alergias, presion, diagnostico, completa, actualizada_en, direccion,
                                emergencia_nombre, emergencia_telefono, antecedentes, medicamentos,
                                antecedentes_odontologicos, examen_extraoral, examen_intraoral, actualizada_por_id)
         SELECT h.paciente_id, h.alergias, h.presion, h.diagnostico, h.completa IS NOT NULL,
                coalesce(h.actualizado, now()), h.direccion, h.emergencia_nombre, h.emergencia_telefono,
                h.antecedentes, h.medicamentos, h.antecedentes_odontologicos, h.examen_extraoral,
                h.examen_intraoral, (SELECT id FROM cuentas WHERE cif = h.actualizado_por)
         FROM anterior.historias h""")
    # Cada cambio de estudiante (trabajo con fecha de fin) es un traspaso al que lo recibió
    q(f"""INSERT INTO traspasos (paciente_id, de_id, a_id, resumen, creado_en)
          SELECT paciente_id, {ID.format('estudiante')}, {ID.format('siguiente')}, resumen, {MEDIODIA.format('hasta')}
          FROM (SELECT t.*, lead(t.estudiante) OVER (PARTITION BY t.paciente_id ORDER BY t.id) AS siguiente
                FROM anterior.trabajo t) x WHERE hasta IS NOT NULL AND siguiente IS NOT NULL""")
    q(f"""INSERT INTO dientes (paciente_id, diente, estado, nota)
          SELECT paciente_id, diente, {mapa('diente', 'estado')}, nota FROM anterior.dientes""")
    q(f"""INSERT INTO fotos (id, paciente_id, tipo, url, creado_en)
          SELECT id, paciente_id, {mapa('foto', 'tipo')}, url, cuando FROM anterior.fotos""")

    # --- Citas: inicio y fin. Antes no tenian duracion: se les da 1 hora, sin pasar de las 16:00 ni encimarse
    #     con la siguiente cita de la misma clinica o del mismo paciente ---
    q(f"""CREATE TEMP TABLE citas_v AS
          SELECT c.*, s.id AS sala_id, (c.fecha || ' ' || c.hora)::timestamp AT TIME ZONE 'America/Managua' AS inicio,
                 {ID.format('p.estudiante')} AS estudiante_id, {ID.format('p.decision_por')} AS aprobador,
                 {mapa('cita', 'c.estado')} AS estado_v
          FROM anterior.citas c JOIN anterior.pacientes p ON p.id = c.paciente_id
          LEFT JOIN anterior.salas s ON s.nombre = c.sala""")
    malas = q("""SELECT id, fecha, hora, sala_id FROM citas_v WHERE sala_id IS NULL
                 OR extract(isodow FROM fecha::date) = 7 OR hora < '07:00' OR hora >= '16:00' ORDER BY id""").fetchall()
    for m in malas:
        motivo = "no tenía clínica" if m["sala_id"] is None else "está fuera del horario de la clínica"
        avisos.append(f"La cita {m['id']} del {m['fecha']} a las {m['hora']} no se pasó: {motivo}"
                      " (el modelo exige clínica y horario de lunes a sábado de 07:00 a 16:00).")
    q("""INSERT INTO citas (id, paciente_id, sala_id, estudiante_id, creado_por_id, aprobado_por_id, aprobado_en,
                            inicio, fin, estado, creado_en, razon, motivo_cancelacion, indicaciones, recordada_en)
         SELECT v.id, v.paciente_id, v.sala_id, v.estudiante_id, v.estudiante_id,
                v.aprobador, CASE WHEN v.aprobador IS NOT NULL THEN now() END, v.inicio,
                least(v.inicio + interval '1 hour',
                      ((v.inicio AT TIME ZONE 'America/Managua')::date + time '16:00') AT TIME ZONE 'America/Managua',
                      (SELECT coalesce(min(w.inicio), 'infinity') FROM citas_v w WHERE w.inicio > v.inicio
                       AND (w.sala_id = v.sala_id OR w.paciente_id = v.paciente_id))),
                CASE WHEN v.aprobador IS NULL AND v.estado_v IN ('confirmada', 'atendida') THEN 'programada'
                     ELSE v.estado_v END,
                v.inicio, v.razon, v.motivo_cancelacion, v.indicaciones, v.recordada
         FROM citas_v v WHERE v.sala_id IS NOT NULL AND extract(isodow FROM v.fecha::date) <> 7
               AND v.hora >= '07:00' AND v.hora < '16:00'""")
    bajadas = uno("SELECT count(*) AS n FROM citas_v WHERE aprobador IS NULL AND estado_v IN ('confirmada', 'atendida')"
                  " AND sala_id IS NOT NULL")["n"]
    if bajadas:
        avisos.append(f"{bajadas} citas confirmadas o atendidas no tenían un profesor que las aprobara: quedaron"
                      " programadas, esperando la aprobación.")
    pendientes = uno("SELECT count(*) AS n FROM citas WHERE aprobado_en IS NULL AND estado = 'programada'")["n"]
    if pendientes:
        avisos.append(f"{pendientes} citas quedaron esperando la aprobación del profesor (su paciente no tenía un"
                      " profesor que lo aprobara).")

    q(f"""INSERT INTO reportes (id, sala_id, tipo, descripcion, reportado_por_id, creado_en, resuelto_por_id, resuelto_en)
          SELECT r.id, r.sala_id, {mapa('falla', 'r.tipo')}, r.motivo,
                 coalesce({ID.format('r.cif')}, {ID.format('r.resuelto_por')}), r.cuando,
                 CASE WHEN r.resuelto_cuando IS NOT NULL THEN {ID.format('r.resuelto_por')} END, r.resuelto_cuando
          FROM anterior.reportes r""")

    # --- Tarifario: el código ahora es obligatorio ---
    q("""INSERT INTO tarifario (id, codigo, nombre, precio, activo, area)
         SELECT id, coalesce(codigo, 'P-' || lpad(id::text, 3, '0')), nombre, precio, activo, area FROM anterior.tarifario""")
    sin_codigo = [r["nombre"] for r in q("SELECT nombre FROM anterior.tarifario WHERE codigo IS NULL")]
    if sin_codigo:
        avisos.append(f"{len(sin_codigo)} procedimientos no tenían código: quedaron con P-<número> (se puede cambiar):"
                      f" {', '.join(sin_codigo)}")
    q("""INSERT INTO precios_anteriores (tarifario_id, precio, vigente_desde, vigente_hasta)
         SELECT procedimiento_id, precio,
                coalesce(lag(hasta) OVER (PARTITION BY procedimiento_id ORDER BY hasta), '-infinity'), hasta
         FROM anterior.precios_anteriores""")
    q(f"""INSERT INTO ordenes (id, paciente_id, creado_por_id, creado_en)
          SELECT id, paciente_id, {ID.format('creada_por')}, creada FROM anterior.ordenes""")

    # --- Tratamientos: lo realizado se liga a la cita de ese dia del paciente ---
    q("""INSERT INTO dientes (paciente_id, diente, estado)
         SELECT DISTINCT paciente_id, diente, 'sano' FROM anterior.tratamientos WHERE diente IS NOT NULL
         ON CONFLICT DO NOTHING""")
    q(f"""INSERT INTO tratamientos (id, paciente_id, tarifario_id, cita_id, diente, orden_id, precio, fase, creado_en)
          SELECT t.id, t.paciente_id, t.procedimiento_id,
                 (SELECT c.id FROM citas c WHERE c.paciente_id = t.paciente_id
                  AND (c.inicio AT TIME ZONE 'America/Managua')::date = t.realizado::date
                  AND c.estado IN ('confirmada', 'atendida') ORDER BY c.inicio DESC LIMIT 1),
                 t.diente, t.orden_id, t.precio, {mapa('fase', 't.fase')}, {MEDIODIA.format('t.anotado')}
          FROM anterior.tratamientos t""")
    sin_cita = q("""SELECT t.id, p.nombre, t.realizado FROM anterior.tratamientos t JOIN anterior.pacientes p
                    ON p.id = t.paciente_id WHERE t.realizado IS NOT NULL
                    AND (SELECT cita_id FROM tratamientos n WHERE n.id = t.id) IS NULL""").fetchall()
    for t in sin_cita:
        avisos.append(f"El tratamiento {t['id']} de {t['nombre']} se realizó el {t['realizado']}, pero ese día no hay"
                      " cita confirmada o atendida del paciente: quedó pendiente (el modelo saca de la cita quién lo"
                      " hizo y cuándo).")

    # --- Abonos -> recibos. En el modelo cada pago es de una orden de cobro: si el paciente no tenía orden se crea
    #     una con sus tratamientos sin orden ---
    for a in q("SELECT a.*, p.estudiante FROM anterior.abonos a JOIN anterior.pacientes p ON p.id = a.paciente_id"
               " ORDER BY a.id").fetchall():
        o = uno("SELECT id, saldo FROM resumen_ordenes WHERE paciente_id = %s AND anulado_en IS NULL AND saldo > 0"
                " ORDER BY id LIMIT 1", a["paciente_id"])
        if not o and uno("SELECT 1 FROM tratamientos WHERE paciente_id = %s AND orden_id IS NULL", a["paciente_id"]):
            nueva = uno(f"INSERT INTO ordenes (paciente_id, creado_por_id, creado_en) VALUES (%s, {ID.format('%s')}, %s)"
                        " RETURNING id", a["paciente_id"], a["cif"], a["cuando"])["id"]
            q("UPDATE tratamientos SET orden_id = %s WHERE paciente_id = %s AND orden_id IS NULL", nueva, a["paciente_id"])
            o = uno("SELECT id, saldo FROM resumen_ordenes WHERE id = %s", nueva)
            avisos.append(f"Se creó la orden de cobro Nº {nueva} del paciente {a['paciente_id']} para su abono de"
                          f" C$ {a['monto']} (en el modelo cada pago es de una orden).")
        if not o:
            avisos.append(f"El abono {a['id']} de C$ {a['monto']} del paciente {a['paciente_id']} no se pasó: no tiene"
                          " tratamientos que cobrar.")
            continue
        pago = min(a["monto"], o["saldo"])
        q(f"""INSERT INTO recibos (orden_id, numero, monto, metodo_pago, pagado_en, estudiante_id)
              VALUES (%s, %s, %s, 'efectivo', %s, {ID.format('%s')})""",
          o["id"], f"ABONO-{a['id']}", pago, a["cuando"], a["estudiante"])
        if pago < a["monto"]:
            avisos.append(f"El abono {a['id']} era de C$ {a['monto']}, más que el saldo de la orden: quedó en C$ {pago}.")
    fotos_recibos = uno("SELECT count(*) AS n FROM anterior.recibos")["n"]
    if fotos_recibos:
        avisos.append(f"{fotos_recibos} fotos de recibos de la hoja de control no se pasaron (en el modelo el recibo es"
                      " el pago de una orden); siguen en el esquema anterior.")

    # --- Inventario: la cantidad pasa a ser una entrada inicial; las entregas, salidas de su solicitud ---
    facultad = uno("SELECT id FROM cuentas WHERE rol = 'facultad' ORDER BY id LIMIT 1")
    q("INSERT INTO materiales (id, nombre, unidad, minimo) SELECT id, nombre, coalesce(nullif(unidad, ''), 'unidad'),"
      " minimo FROM anterior.materiales")
    q(f"""INSERT INTO solicitudes (id, material_id, estudiante_id, cantidad, estado, creado_en, para, nota, atendida_por_id)
          SELECT id, material_id, {ID.format('estudiante')}, cantidad, {mapa('solicitud', 'estado')}, cuando,
                 para::date, nota, {ID.format('atendida_por')} FROM anterior.solicitudes""")
    if facultad:
        q("""INSERT INTO movimientos_inventario (material_id, tipo, cantidad, responsable_id)
             SELECT m.id, 'entrada', m.cantidad + coalesce((SELECT sum(s.cantidad) FROM anterior.solicitudes s
                    WHERE s.material_id = m.id AND s.estado = 'Entregada'), 0), %s
             FROM anterior.materiales m WHERE m.cantidad + coalesce((SELECT sum(s.cantidad) FROM anterior.solicitudes s
                    WHERE s.material_id = m.id AND s.estado = 'Entregada'), 0) > 0""", facultad["id"])
        q("""INSERT INTO movimientos_inventario (material_id, solicitud_id, tipo, cantidad, responsable_id)
             SELECT material_id, id, 'salida', cantidad, coalesce(atendida_por_id, %s) FROM solicitudes
             WHERE estado = 'aprobada'""", facultad["id"])

    q("""INSERT INTO pedidos_contacto (id, paciente_id, pedido_en, atendido_en, atendido_por_id)
         SELECT k.id, k.paciente_id, k.pedido, k.atendido, (SELECT id FROM cuentas WHERE cif = k.atendido_por)
         FROM anterior.pedidos_contacto k""")

    # Los numeros automaticos siguen despues del ultimo que se paso
    for s in q("SELECT table_name AS t, column_name AS col FROM information_schema.columns WHERE table_schema ="
               " 'public' AND column_default LIKE 'nextval%'").fetchall():
        q(f'SELECT setval(pg_get_serial_sequence(%s, %s), coalesce((SELECT max("{s["col"]}") FROM'
          f' public."{s["t"]}"), 0) + 1, false)', f'public."{s["t"]}"', s["col"])
    return avisos


if __name__ == "__main__":
    url = os.environ.get("MIGRAR_DATABASE_URL")
    if not url:
        sys.exit("Falta MIGRAR_DATABASE_URL: la cadena de la rama nueva de Neon (nunca la de producción).")
    with psycopg.connect(url, row_factory=dict_row, prepare_threshold=None) as conexion:
        with conexion.transaction():
            avisos = migrar(conexion)
        cuantas = {t: conexion.execute(f"SELECT count(*) AS n FROM public.{t}").fetchone()["n"]
                   for t in ("cuentas", "clases", "pacientes", "historias", "citas", "tratamientos", "ordenes",
                             "recibos", "bitacora", "salas", "reportes", "tarifario")}
    print("Migración al modelo v14 terminada.\n" + ", ".join(f"{t} {n}" for t, n in cuantas.items()))
    print("\nLo que no pasó tal cual:" if avisos else "\nTodo pasó tal cual.")
    for a in avisos:
        print(" -", a)
    print("\nLas tablas viejas quedaron en el esquema 'anterior'. Cuando todo esté revisado:"
          " DROP SCHEMA anterior CASCADE;")
