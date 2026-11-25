-- Clínicas Odontológicas · Modelo de datos versión 14 (PostgreSQL)
-- 20 tablas. Todas las relaciones son 1:N, excepto pacientes–historias y solicitudes–movimientos_inventario (1:1).
-- Ninguna N:M, ni directa ni simulada con una tabla intermedia (por eso un estudiante está en UNA clase a la vez).
-- Sin datos repetidos, salvo dos copias históricas a propósito: tratamientos.precio y bitacora.cif_registrado.
-- Las columnas text que son listas de valores (rol, estado, tipo, fase, metodo_pago, superficie, sexo) llevan CHECK
-- (en el diagrama se marcan con *).
-- Fechas: *_en = timestamptz (momento exacto).
-- Horario de la clínica: lunes a sábado, de 07:00 a 16:00 (hora de Managua). Lo cumplen turnos y citas.
-- Esta es la base que usa el sistema: el modelo v14 tal cual, con tres ajustes marcados con [ajuste]
-- y, al final, las EXTENSIONES: columnas y tablas que no están en el diagrama y que conservan funciones
-- que el sistema ya tenía (consentimiento, bloqueo de cuentas, recordatorios por WhatsApp, etc.).
-- El programa lo ejecuta una sola vez, en una base vacía (ver crear_tablas en prototipo.py).

CREATE EXTENSION IF NOT EXISTS btree_gist;   -- para impedir horarios y periodos encimados

-- ============ Usuarios, clases y horarios ============
-- Una sola tabla de cuentas para todos; el tipo de cuenta lo dice la columna rol:
--   estudiante: tiene anio (año de la carrera); pertenece a UNA clase a la vez (cuentas.clase_id), a la que entra con
--               un código QR; si otro profesor le da su QR, pasa a esa clase; agenda sus citas
--               (necesitan la aprobación de su profesor), atiende pacientes, tiene turnos y pide material.
--   profesor:   imparte clases, genera los códigos QR de su clase, agenda citas para sus estudiantes (quedan aprobadas),
--               aprueba citas y tratamientos de SUS estudiantes y firma recibos.
--   facultad:   tiene el control de todo lo demás y puede VER todo: cuentas, tarifario, órdenes, inventario,
--               salas y reportes. (Qué ve cada rol en pantalla lo controla el programa; ver el final del archivo.)

CREATE TABLE cuentas (
    id          serial PRIMARY KEY,                                -- llave interna que nunca cambia
    cif         text NOT NULL UNIQUE,                              -- carné; único, pero se puede corregir
    nombre      text NOT NULL,
    correo      text NOT NULL UNIQUE CHECK (correo LIKE '%_@_%'),
    anio        smallint CHECK (anio BETWEEN 1 AND 6),             -- año de la carrera
    rol         text NOT NULL CHECK (rol IN ('estudiante', 'profesor', 'facultad')),
    clave_hash  text NOT NULL,                                     -- nunca la contraseña en texto plano
    activa      boolean NOT NULL DEFAULT true,                     -- las cuentas se desactivan, no se borran
    clase_id    int,                                               -- clase actual del estudiante (FK se agrega abajo)
    creado_en   timestamptz NOT NULL DEFAULT now(),
    CHECK ((rol = 'estudiante') = (anio IS NOT NULL)),             -- el estudiante SIEMPRE tiene anio; los demás nunca
    CHECK (clase_id IS NULL OR rol = 'estudiante')                 -- solo el estudiante pertenece a una clase
);

CREATE TABLE clases (
    id           serial PRIMARY KEY,
    nombre       text NOT NULL,
    periodo      text NOT NULL CHECK (periodo ~ '^[0-9]{4}-(I|II|III)$'),   -- p. ej. '2026-II'
    profesor_id  int NOT NULL REFERENCES cuentas (id) ON DELETE RESTRICT,
    creado_en    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (nombre, periodo, profesor_id)
);

ALTER TABLE cuentas ADD CONSTRAINT cuentas_clase_fk
    FOREIGN KEY (clase_id) REFERENCES clases (id) ON DELETE SET NULL
    DEFERRABLE INITIALLY DEFERRED;  -- [ajuste 1] cuentas y clases se apuntan entre sí: así un respaldo se carga en cualquier orden

-- Invitación = código QR de una clase. Se usa una sola vez: quien la usa pasa a esa clase (cuentas.clase_id).
-- No guarda al estudiante: así NO une estudiantes con clases (no simula un muchos a muchos).
-- Quién la usó queda en la bitácora.
CREATE TABLE invitaciones (
    id         serial PRIMARY KEY,
    codigo     text NOT NULL UNIQUE,                               -- lo que va dentro del QR
    clase_id   int NOT NULL REFERENCES clases (id) ON DELETE CASCADE,
    vence_en   timestamptz NOT NULL,
    usada_en   timestamptz,                                        -- vacío = todavía no se usa
    creado_en  timestamptz NOT NULL DEFAULT now(),
    CHECK (vence_en > creado_en)
);

CREATE TABLE turnos (
    id             serial PRIMARY KEY,
    estudiante_id  int NOT NULL REFERENCES cuentas (id) ON DELETE CASCADE,
    dia            smallint NOT NULL,
    desde          time NOT NULL,
    hasta          time NOT NULL,
    CONSTRAINT turno_dia_valido     CHECK (dia BETWEEN 1 AND 6),                          -- 1 = lunes … 6 = sábado
    CONSTRAINT turno_inicio_antes   CHECK (desde < hasta),                                 -- desde siempre menor que hasta
    CONSTRAINT turno_en_horario     CHECK (desde >= time '07:00' AND hasta <= time '16:00'),  -- dentro del horario de la clínica
    -- un estudiante no puede tener dos turnos que se superpongan el mismo día
    -- (08:00–10:00 y 09:00–11:00 se rechaza; 08:00–10:00 y 10:00–12:00 sí se permite, porque el rango es [desde, hasta) )
    CONSTRAINT turno_sin_superposicion EXCLUDE USING gist (
        estudiante_id WITH =, dia WITH =,
        tsrange(date '2000-01-03' + desde, date '2000-01-03' + hasta) WITH &&)
);

-- Bitácora: cuenta_id es FK (no se puede registrar a alguien que no existe) y
-- cif_registrado conserva quién fue aunque la cuenta se llegara a borrar.
CREATE TABLE bitacora (
    id              serial PRIMARY KEY,
    cuenta_id       int REFERENCES cuentas (id) ON DELETE SET NULL,
    cif_registrado  text NOT NULL,                                 -- copia histórica A PROPÓSITO (no es redundancia):
                                                                   -- conserva quién fue aunque la cuenta se borre o cambie de CIF
    accion          text NOT NULL,
    detalle         text,
    creado_en       timestamptz NOT NULL DEFAULT now()
);

-- ============ Pacientes y atención clínica ============

CREATE TABLE pacientes (
    id             serial PRIMARY KEY,
    cedula         text NOT NULL UNIQUE,
    expediente     text NOT NULL UNIQUE,
    nombre         text NOT NULL,
    nacimiento     date CHECK (nacimiento <= current_date),
    sexo           char(1) CHECK (sexo IN ('F', 'M')),
    telefono       text,
    estudiante_id  int REFERENCES cuentas (id) ON DELETE RESTRICT, -- estudiante a cargo ahora
    aprobado_por_id int REFERENCES cuentas (id) ON DELETE RESTRICT,-- profesor que lo aceptó para ser atendido
    aprobado_en    timestamptz,                                    -- vacío = todavía no aprobado
    creado_en      timestamptz NOT NULL DEFAULT now(),
    CHECK ((aprobado_por_id IS NULL) = (aprobado_en IS NULL))
);

CREATE TABLE historias (
    paciente_id  int PRIMARY KEY REFERENCES pacientes (id) ON DELETE CASCADE,   -- PK y FK: uno a uno
    alergias     text,
    presion      text,
    diagnostico  text,
    completa        boolean NOT NULL DEFAULT false,
    actualizada_en  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE dientes (
    paciente_id  int NOT NULL REFERENCES pacientes (id) ON DELETE CASCADE,
    diente       smallint NOT NULL CHECK (                       -- notación FDI
                     (diente / 10 BETWEEN 1 AND 4 AND diente % 10 BETWEEN 1 AND 8)    -- permanentes 11–48
                  OR (diente / 10 BETWEEN 5 AND 8 AND diente % 10 BETWEEN 1 AND 5)),  -- temporales 51–85
    estado       text NOT NULL CHECK (estado IN ('sano', 'caries', 'obturado', 'corona', 'endodoncia', 'ausente', 'extraccion')),
    PRIMARY KEY (paciente_id, diente)                               -- PK compuesta; paciente_id además es FK
);

CREATE TABLE fotos (
    id           serial PRIMARY KEY,
    paciente_id  int NOT NULL REFERENCES pacientes (id) ON DELETE CASCADE,
    tipo         text NOT NULL CHECK (tipo IN ('intraoral', 'extraoral', 'radiografia')),
    url          text NOT NULL,
    descripcion  text,
    creado_en    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE salas (
    id          serial PRIMARY KEY,
    nombre      text NOT NULL UNIQUE,
    habilitada  boolean NOT NULL DEFAULT true                      -- en servicio o no; lo libre/ocupado sale de citas
);

CREATE TABLE citas (
    id           serial PRIMARY KEY,
    paciente_id  int NOT NULL REFERENCES pacientes (id) ON DELETE RESTRICT,
    sala_id      int NOT NULL REFERENCES salas (id) ON DELETE RESTRICT,
    estudiante_id int NOT NULL REFERENCES cuentas (id) ON DELETE RESTRICT,  -- quién atiende ESA cita
    creado_por_id int NOT NULL REFERENCES cuentas (id) ON DELETE RESTRICT,  -- quién la agendó: estudiante o profesor
    aprobado_por_id int REFERENCES cuentas (id) ON DELETE RESTRICT,         -- profesor que la aprobó
    aprobado_en  timestamptz,                                      -- vacío = falta la aprobación del profesor
    inicio       timestamptz NOT NULL,
    fin          timestamptz NOT NULL,
    estado       text NOT NULL DEFAULT 'programada'
                 CHECK (estado IN ('programada', 'confirmada', 'atendida', 'cancelada', 'no_asistio')),
    creado_en    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT cita_inicio_antes CHECK (fin > inicio AND fin <= inicio + interval '4 hours'),
    -- horario de atención: lunes (1) a sábado (6), de 07:00 a 16:00 hora de Managua, empieza y termina el mismo día
    CONSTRAINT cita_dia_valido CHECK (extract(isodow FROM inicio AT TIME ZONE 'America/Managua') BETWEEN 1 AND 6),
    CONSTRAINT cita_en_horario CHECK (
        (inicio AT TIME ZONE 'America/Managua')::time >= time '07:00'
        AND (fin AT TIME ZONE 'America/Managua')::time <= time '16:00'
        AND (inicio AT TIME ZONE 'America/Managua')::date = (fin AT TIME ZONE 'America/Managua')::date),
    CHECK ((aprobado_por_id IS NULL) = (aprobado_en IS NULL)),
    CONSTRAINT cita_necesita_aprobacion CHECK (estado NOT IN ('confirmada', 'atendida') OR aprobado_en IS NOT NULL),
    UNIQUE (id, paciente_id),                                      -- para la FK compuesta desde tratamientos
    -- UNA SALA NO PUEDE TENER DOS CITAS AL MISMO TIEMPO (las canceladas y las de quien no asistió liberan la sala)
    CONSTRAINT cita_sala_sin_superposicion EXCLUDE USING gist (sala_id WITH =, tstzrange(inicio, fin) WITH &&)
        WHERE (estado IN ('programada', 'confirmada', 'atendida')),
    CONSTRAINT cita_paciente_sin_superposicion EXCLUDE USING gist (paciente_id WITH =, tstzrange(inicio, fin) WITH &&)
        WHERE (estado IN ('programada', 'confirmada', 'atendida'))
    -- (el estudiante NO se valida aquí: lo que no puede repetirse es la sala)
);

CREATE TABLE reportes (
    id               serial PRIMARY KEY,
    sala_id          int NOT NULL REFERENCES salas (id) ON DELETE CASCADE,
    tipo             text NOT NULL CHECK (tipo IN ('equipo', 'electricidad', 'agua', 'mobiliario', 'limpieza', 'otro')),
    descripcion      text,
    reportado_por_id int NOT NULL REFERENCES cuentas (id) ON DELETE RESTRICT,
    creado_en        timestamptz NOT NULL DEFAULT now(),
    resuelto_por_id  int REFERENCES cuentas (id) ON DELETE RESTRICT,
    resuelto_en      timestamptz,
    CHECK ((resuelto_por_id IS NULL) = (resuelto_en IS NULL)),
    CHECK (resuelto_en IS NULL OR resuelto_en >= creado_en)
);

-- ============ Tratamientos, precios y pagos ============

CREATE TABLE tarifario (
    id      serial PRIMARY KEY,
    codigo  text NOT NULL UNIQUE,
    nombre  text NOT NULL,
    precio  numeric(10,2) NOT NULL CHECK (precio >= 0),
    activo  boolean NOT NULL DEFAULT true                          -- un procedimiento se retira, no se borra
);

CREATE TABLE precios_anteriores (
    id            serial PRIMARY KEY,
    tarifario_id  int NOT NULL REFERENCES tarifario (id) ON DELETE CASCADE,
    precio        numeric(10,2) NOT NULL CHECK (precio >= 0),
    vigente_desde timestamptz NOT NULL,                            -- incluido
    vigente_hasta timestamptz NOT NULL,                            -- no incluido
    CHECK (vigente_desde < vigente_hasta),
    EXCLUDE USING gist (tarifario_id WITH =, tstzrange(vigente_desde, vigente_hasta) WITH &&)   -- sin periodos encimados
);

CREATE TABLE ordenes (
    id             serial PRIMARY KEY,
    paciente_id    int NOT NULL REFERENCES pacientes (id) ON DELETE RESTRICT,
    creado_por_id  int NOT NULL REFERENCES cuentas (id) ON DELETE RESTRICT,
    creado_en      timestamptz NOT NULL DEFAULT now(),
    anulado_en     timestamptz,                                    -- vacío = vigente
    UNIQUE (id, paciente_id)                                       -- para la FK compuesta desde tratamientos
    -- total, pagado, saldo y estado NO se guardan: los calcula la vista resumen_ordenes
);

-- Un tratamiento es un procedimiento sobre un paciente, opcionalmente sobre un diente y una cara del diente.
-- Quién lo hizo y cuándo sale de la cita en que se hizo (cita_id); sin cita = pendiente.
CREATE TABLE tratamientos (
    id               serial PRIMARY KEY,
    paciente_id      int NOT NULL REFERENCES pacientes (id) ON DELETE RESTRICT,
    tarifario_id     int NOT NULL REFERENCES tarifario (id) ON DELETE RESTRICT,
    cita_id          int,                                          -- cita en que se hizo; vacío = pendiente
    diente           smallint,                                     -- vacío si es de toda la boca (p. ej. limpieza)
    superficie       text CHECK (superficie IN ('oclusal', 'mesial', 'distal', 'vestibular', 'lingual')),
    orden_id         int,                                          -- orden en que se cobra
    aprobado_por_id  int REFERENCES cuentas (id) ON DELETE RESTRICT,   -- profesor
    precio           numeric(10,2) NOT NULL CHECK (precio >= 0),   -- copia histórica A PROPÓSITO: precio cobrado ese día
    descuento_monto  numeric(10,2) NOT NULL DEFAULT 0 CHECK (descuento_monto >= 0),   -- en córdobas, NO porcentaje
    fase             text NOT NULL CHECK (fase IN ('urgencia', 'basica', 'rehabilitacion', 'mantenimiento')),
    creado_en        timestamptz NOT NULL DEFAULT now(),
    CHECK (descuento_monto <= precio),
    CHECK (superficie IS NULL OR diente IS NOT NULL),              -- no hay cara sin diente
    -- FK compuestas: garantizan que el diente, la cita y la orden sean DEL MISMO paciente
    FOREIGN KEY (paciente_id, diente)  REFERENCES dientes (paciente_id, diente) ON DELETE RESTRICT,
    FOREIGN KEY (cita_id, paciente_id) REFERENCES citas (id, paciente_id)      ON DELETE RESTRICT,
    FOREIGN KEY (orden_id, paciente_id) REFERENCES ordenes (id, paciente_id)   ON DELETE RESTRICT
);

-- Cada pago (abono) a una orden es un recibo.
CREATE TABLE recibos (
    id              serial PRIMARY KEY,
    orden_id        int NOT NULL REFERENCES ordenes (id) ON DELETE RESTRICT,
    numero          text NOT NULL UNIQUE,
    monto           numeric(10,2) NOT NULL CHECK (monto > 0),
    metodo_pago     text NOT NULL CHECK (metodo_pago IN ('efectivo', 'tarjeta', 'transferencia')),
    referencia      text,                                          -- nº de transferencia o voucher
    firmado_por_id  int REFERENCES cuentas (id) ON DELETE RESTRICT,
    pagado_en       timestamptz NOT NULL DEFAULT now(),
    anulado_en      timestamptz,                                   -- vacío = válido
    CHECK (metodo_pago = 'efectivo' OR referencia IS NOT NULL)
);

-- ============ Inventario ============

CREATE TABLE materiales (
    id      serial PRIMARY KEY,
    nombre  text NOT NULL UNIQUE,
    unidad  text NOT NULL DEFAULT 'unidad',                        -- unidad, caja, ml…
    minimo  int NOT NULL DEFAULT 0 CHECK (minimo >= 0),
    activo  boolean NOT NULL DEFAULT true
    -- la cantidad en bodega NO se guarda: la calcula la vista existencias
);

CREATE TABLE solicitudes (
    id             serial PRIMARY KEY,
    material_id    int NOT NULL REFERENCES materiales (id) ON DELETE RESTRICT,
    estudiante_id  int NOT NULL REFERENCES cuentas (id) ON DELETE RESTRICT,
    cantidad       int NOT NULL CHECK (cantidad > 0),
    estado         text NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'aprobada', 'rechazada')),
    creado_en      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, material_id)                                       -- para la FK compuesta desde movimientos
    -- "entregada" no se guarda: una solicitud está entregada si tiene su movimiento de salida
);

CREATE TABLE movimientos_inventario (
    id              serial PRIMARY KEY,
    material_id     int NOT NULL REFERENCES materiales (id) ON DELETE RESTRICT,
    solicitud_id    int UNIQUE,                                    -- UK: una solicitud se entrega UNA sola vez (1:1)
    tipo            text NOT NULL CHECK (tipo IN ('entrada', 'salida', 'ajuste')),
    cantidad        int NOT NULL CHECK (cantidad <> 0),            -- entrada y salida > 0; ajuste puede ser ±
    responsable_id  int NOT NULL REFERENCES cuentas (id) ON DELETE RESTRICT,
    creado_en       timestamptz NOT NULL DEFAULT now(),
    CHECK (tipo = 'ajuste' OR cantidad > 0),
    CHECK (solicitud_id IS NULL OR tipo = 'salida'),
    -- FK compuesta: el material del movimiento TIENE que ser el de la solicitud (no pueden diferir)
    FOREIGN KEY (solicitud_id, material_id) REFERENCES solicitudes (id, material_id) ON DELETE RESTRICT
);

-- ============ Índices en las llaves foráneas ============

CREATE INDEX ON clases (profesor_id);
CREATE INDEX ON invitaciones (clase_id);
CREATE INDEX ON cuentas (clase_id);
CREATE INDEX ON turnos (estudiante_id);
CREATE INDEX ON bitacora (cuenta_id, creado_en);
CREATE INDEX ON pacientes (estudiante_id);
CREATE INDEX ON pacientes (aprobado_por_id);
CREATE INDEX ON fotos (paciente_id);
CREATE INDEX ON citas (paciente_id);
CREATE INDEX ON citas (sala_id);
CREATE INDEX ON citas (estudiante_id);
CREATE INDEX ON citas (creado_por_id);
CREATE INDEX ON citas (aprobado_por_id);
CREATE INDEX ON reportes (sala_id);
CREATE INDEX ON reportes (reportado_por_id);
CREATE INDEX ON reportes (resuelto_por_id);
CREATE INDEX ON precios_anteriores (tarifario_id);
CREATE INDEX ON tratamientos (paciente_id);
CREATE INDEX ON tratamientos (tarifario_id);
CREATE INDEX ON tratamientos (orden_id);
CREATE INDEX ON tratamientos (cita_id);
CREATE INDEX ON tratamientos (paciente_id, diente);
CREATE INDEX ON ordenes (paciente_id);
CREATE INDEX ON ordenes (creado_por_id);
CREATE INDEX ON tratamientos (aprobado_por_id);
CREATE INDEX ON recibos (orden_id);
CREATE INDEX ON recibos (firmado_por_id);
CREATE INDEX ON solicitudes (material_id);
CREATE INDEX ON solicitudes (estudiante_id);
CREATE INDEX ON movimientos_inventario (material_id);
CREATE INDEX ON movimientos_inventario (responsable_id);

-- ============ Datos calculados (no se guardan, así no se repiten) ============

CREATE VIEW resumen_ordenes AS
SELECT o.id,
       o.paciente_id,
       o.creado_en,
       o.anulado_en,
       COALESCE(t.total, 0)                         AS total,
       COALESCE(r.pagado, 0)                        AS pagado,
       COALESCE(t.total, 0) - COALESCE(r.pagado, 0) AS saldo,
       CASE WHEN o.anulado_en IS NOT NULL THEN 'anulada'
            WHEN COALESCE(t.total, 0) > 0 AND COALESCE(r.pagado, 0) >= t.total THEN 'pagada'
            WHEN COALESCE(r.pagado, 0) > 0 THEN 'abonada'
            ELSE 'pendiente' END                    AS estado
FROM ordenes o
LEFT JOIN (SELECT orden_id, SUM(precio - descuento_monto) AS total
           FROM tratamientos GROUP BY orden_id) t ON t.orden_id = o.id
LEFT JOIN (SELECT orden_id, SUM(monto) AS pagado
           FROM recibos WHERE anulado_en IS NULL GROUP BY orden_id) r ON r.orden_id = o.id;

-- Tratamientos YA hechos (cita atendida): quién lo hizo y cuándo sale de la cita.
-- Historial por diente: filtrar por paciente y diente.
CREATE VIEW tratamientos_realizados AS
SELECT t.id, t.paciente_id, t.diente, t.superficie, tf.nombre AS procedimiento,
       c.estudiante_id AS realizado_por_id, c.inicio AS realizado_en, t.aprobado_por_id
FROM tratamientos t
JOIN tarifario tf ON tf.id = t.tarifario_id
JOIN citas c ON c.id = t.cita_id AND c.estado = 'atendida';   -- solo lo que ya se hizo

CREATE VIEW existencias AS
SELECT m.id, m.nombre, m.unidad, m.minimo,
       COALESCE(SUM(CASE WHEN mv.tipo = 'salida' THEN -mv.cantidad ELSE mv.cantidad END), 0) AS cantidad,
       COALESCE(SUM(CASE WHEN mv.tipo = 'salida' THEN -mv.cantidad ELSE mv.cantidad END), 0) < m.minimo AS reponer
FROM materiales m
LEFT JOIN movimientos_inventario mv ON mv.material_id = m.id
GROUP BY m.id;

-- ============ Reglas automáticas (triggers) ============

-- 1) La persona de una columna debe tener el rol correcto y estar activa.
--    Uso: revisar_rol('<columna>', '<rol>')  ó  revisar_rol('<columna>', '<rol1>|<rol2>')
-- [ajuste 2] Se revisa al crear la fila o cuando cambia esa columna: si no, desactivar a un profesor impedía
--            tocar después cualquier cita o tratamiento que él aprobó. Al cargar un respaldo no se revisa
--            (los datos ya se revisaron cuando se guardaron): lo indica clinicas.restaurando = 'si'.
CREATE FUNCTION restaurando() RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT coalesce(current_setting('clinicas.restaurando', true), '') = 'si'
$$;

CREATE FUNCTION revisar_rol() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_id int := (to_jsonb(NEW) ->> TG_ARGV[0])::int;
BEGIN
    IF restaurando() OR (TG_OP = 'UPDATE' AND v_id IS NOT DISTINCT FROM (to_jsonb(OLD) ->> TG_ARGV[0])::int) THEN
        RETURN NEW;
    END IF;
    IF v_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM cuentas WHERE id = v_id AND rol = ANY (string_to_array(TG_ARGV[1], '|')) AND activa) THEN
        RAISE EXCEPTION '%.%: la cuenta % no tiene el rol % o está desactivada', TG_TABLE_NAME, TG_ARGV[0], v_id, TG_ARGV[1];
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER rol_profesor     BEFORE INSERT OR UPDATE ON clases       FOR EACH ROW EXECUTE FUNCTION revisar_rol('profesor_id', 'profesor');
CREATE TRIGGER rol_estudiante   BEFORE INSERT OR UPDATE ON turnos       FOR EACH ROW EXECUTE FUNCTION revisar_rol('estudiante_id', 'estudiante');
CREATE TRIGGER rol_estudiante   BEFORE INSERT OR UPDATE ON pacientes    FOR EACH ROW EXECUTE FUNCTION revisar_rol('estudiante_id', 'estudiante');
CREATE TRIGGER rol_aprobado     BEFORE INSERT OR UPDATE ON pacientes    FOR EACH ROW EXECUTE FUNCTION revisar_rol('aprobado_por_id', 'profesor');
CREATE TRIGGER rol_estudiante   BEFORE INSERT OR UPDATE ON citas        FOR EACH ROW EXECUTE FUNCTION revisar_rol('estudiante_id', 'estudiante');
CREATE TRIGGER rol_creador      BEFORE INSERT OR UPDATE ON citas        FOR EACH ROW EXECUTE FUNCTION revisar_rol('creado_por_id', 'estudiante|profesor');
CREATE TRIGGER rol_aprobado     BEFORE INSERT OR UPDATE ON citas        FOR EACH ROW EXECUTE FUNCTION revisar_rol('aprobado_por_id', 'profesor');
CREATE TRIGGER rol_resuelto     BEFORE INSERT OR UPDATE ON reportes     FOR EACH ROW EXECUTE FUNCTION revisar_rol('resuelto_por_id', 'facultad');
CREATE TRIGGER rol_estudiante   BEFORE INSERT OR UPDATE ON solicitudes  FOR EACH ROW EXECUTE FUNCTION revisar_rol('estudiante_id', 'estudiante');
CREATE TRIGGER rol_aprobado     BEFORE INSERT OR UPDATE ON tratamientos FOR EACH ROW EXECUTE FUNCTION revisar_rol('aprobado_por_id', 'profesor');
CREATE TRIGGER rol_firmado      BEFORE INSERT OR UPDATE ON recibos      FOR EACH ROW EXECUTE FUNCTION revisar_rol('firmado_por_id', 'profesor');
CREATE TRIGGER rol_responsable  BEFORE INSERT OR UPDATE ON movimientos_inventario FOR EACH ROW EXECUTE FUNCTION revisar_rol('responsable_id', 'facultad');
CREATE TRIGGER rol_creador      BEFORE INSERT OR UPDATE ON ordenes      FOR EACH ROW EXECUTE FUNCTION revisar_rol('creado_por_id', 'profesor|facultad');

-- 2) Usar un código QR: en UNA sola operación marca el código como usado, pone al estudiante en esa clase
--    y lo anota en la bitácora. Un código se usa una sola vez y antes de vencer.
--    Uso desde el programa:  SELECT usar_invitacion('QR-7K2', <id de la cuenta>);
CREATE FUNCTION usar_invitacion(p_codigo text, p_cuenta int) RETURNS int LANGUAGE plpgsql AS $$
DECLARE
    v_inv invitaciones%ROWTYPE;
    v_cta cuentas%ROWTYPE;
BEGIN
    SELECT * INTO v_cta FROM cuentas WHERE id = p_cuenta;
    IF v_cta.rol IS DISTINCT FROM 'estudiante' OR NOT v_cta.activa THEN
        RAISE EXCEPTION 'Solo un estudiante activo puede usar un código de invitación';
    END IF;
    SELECT * INTO v_inv FROM invitaciones WHERE codigo = p_codigo FOR UPDATE;   -- bloquea: nadie más lo usa a la vez
    IF NOT FOUND THEN RAISE EXCEPTION 'El código % no existe', p_codigo; END IF;
    IF v_inv.usada_en IS NOT NULL THEN RAISE EXCEPTION 'El código % ya fue usado', p_codigo; END IF;
    IF v_inv.vence_en <= now() THEN RAISE EXCEPTION 'El código % ya venció', p_codigo; END IF;
    UPDATE invitaciones SET usada_en = now() WHERE id = v_inv.id;
    UPDATE cuentas SET clase_id = v_inv.clase_id WHERE id = p_cuenta;
    INSERT INTO bitacora (cuenta_id, cif_registrado, accion, detalle)
    VALUES (p_cuenta, v_cta.cif, 'usó código de invitación',
            format('código %s · clase %s (antes: %s)', p_codigo, v_inv.clase_id, COALESCE(v_cta.clase_id::text, 'ninguna')));
    RETURN v_inv.clase_id;
END $$;

-- Un código ya usado no se "des-usa" ni se cambia de clase.
CREATE FUNCTION invitacion_fija() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.usada_en IS NOT NULL THEN
        RAISE EXCEPTION 'La invitación % ya fue usada y no se modifica', OLD.codigo;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER invitacion_usada BEFORE UPDATE ON invitaciones
    FOR EACH ROW EXECUTE FUNCTION invitacion_fija();

-- 3) El profesor solo tiene poder sobre los estudiantes de SUS clases:
--    solo aprueba un tratamiento si el estudiante que lo hizo (el de la cita) entró a una de sus clases.
CREATE FUNCTION revisar_profesor_del_estudiante() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_est int;
BEGIN
    IF restaurando() THEN RETURN NEW; END IF;
    SELECT estudiante_id INTO v_est FROM citas WHERE id = NEW.cita_id;
    IF NEW.aprobado_por_id IS NOT NULL AND v_est IS NOT NULL AND NOT es_profesor_de(NEW.aprobado_por_id, v_est) THEN
        RAISE EXCEPTION 'El profesor % no tiene en sus clases al estudiante %', NEW.aprobado_por_id, v_est;
    END IF;
    IF NEW.cita_id IS NOT NULL AND (SELECT estado FROM citas WHERE id = NEW.cita_id) IN ('cancelada', 'no_asistio') THEN
        RAISE EXCEPTION 'La cita % no se realizó', NEW.cita_id;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER profesor_del_estudiante BEFORE INSERT OR UPDATE OF aprobado_por_id, cita_id ON tratamientos
    FOR EACH ROW EXECUTE FUNCTION revisar_profesor_del_estudiante();

-- 3a) Citas: las agenda un estudiante o un profesor.
--     · Si la agenda un ESTUDIANTE, es una cita suya y queda pendiente hasta que la apruebe un profesor de sus clases.
--     · Si la agenda un PROFESOR, tiene que ser para un estudiante de sus clases y queda aprobada al crearse.
--     · Quien aprueba siempre tiene que ser profesor de una clase del estudiante.
CREATE FUNCTION es_profesor_de(p_profesor int, p_estudiante int) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM cuentas e JOIN clases c ON c.id = e.clase_id
                   WHERE e.id = p_estudiante AND c.profesor_id = p_profesor)   -- profesor de la clase ACTUAL del estudiante
$$;

CREATE FUNCTION revisar_cita() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_rol text;
BEGIN
    IF restaurando() THEN RETURN NEW; END IF;
    SELECT rol INTO v_rol FROM cuentas WHERE id = NEW.creado_por_id;
    IF v_rol = 'estudiante' AND NEW.estudiante_id <> NEW.creado_por_id THEN
        RAISE EXCEPTION 'Un estudiante solo puede agendar sus propias citas';
    END IF;
    IF TG_OP = 'INSERT' AND v_rol = 'profesor' THEN
        IF NOT es_profesor_de(NEW.creado_por_id, NEW.estudiante_id) THEN
            RAISE EXCEPTION 'El profesor % no tiene en sus clases al estudiante %', NEW.creado_por_id, NEW.estudiante_id;
        END IF;
        NEW.aprobado_por_id := NEW.creado_por_id;                  -- la cita del profesor ya va aprobada
        NEW.aprobado_en := now();
    END IF;
    -- [ajuste 3] la aprobación se revisa cuando se da: si después el estudiante cambia de clase, su cita
    --            ya aprobada se puede seguir marcando como confirmada o atendida
    IF NEW.aprobado_por_id IS NOT NULL AND (TG_OP = 'INSERT' OR NEW.aprobado_por_id IS DISTINCT FROM OLD.aprobado_por_id)
       AND NOT es_profesor_de(NEW.aprobado_por_id, NEW.estudiante_id) THEN
        RAISE EXCEPTION 'El profesor % no tiene en sus clases al estudiante %', NEW.aprobado_por_id, NEW.estudiante_id;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.aprobado_por_id IS NOT NULL AND OLD.aprobado_por_id IS NULL AND NEW.aprobado_en IS NULL THEN
        NEW.aprobado_en := now();
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER cita_aprobacion BEFORE INSERT OR UPDATE ON citas
    FOR EACH ROW EXECUTE FUNCTION revisar_cita();

-- 3b) Una cita que ya tiene tratamientos no se puede pasar a cancelada ni a no_asistio
--     (el trigger 3 lo revisa desde tratamientos; este, desde citas).
CREATE FUNCTION revisar_cancelacion_cita() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.estado IN ('cancelada', 'no_asistio') AND OLD.estado NOT IN ('cancelada', 'no_asistio')
       AND EXISTS (SELECT 1 FROM tratamientos WHERE cita_id = NEW.id) THEN
        RAISE EXCEPTION 'La cita % tiene tratamientos registrados; no se puede marcar como %', NEW.id, NEW.estado;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER cita_con_tratamientos BEFORE UPDATE OF estado ON citas
    FOR EACH ROW EXECUTE FUNCTION revisar_cancelacion_cita();

-- 4) A una orden anulada no se le agregan tratamientos.
--    (Que la orden sea del mismo paciente ya lo garantiza la FK compuesta.)
CREATE FUNCTION revisar_orden_tratamiento() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.orden_id IS NOT NULL AND (SELECT anulado_en FROM ordenes WHERE id = NEW.orden_id) IS NOT NULL THEN
        RAISE EXCEPTION 'La orden % está anulada', NEW.orden_id;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER orden_no_anulada BEFORE INSERT OR UPDATE OF orden_id ON tratamientos
    FOR EACH ROW EXECUTE FUNCTION revisar_orden_tratamiento();

-- 5) No se puede pagar más de lo que debe la orden, ni pagar una orden anulada.
CREATE FUNCTION revisar_pago() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_saldo numeric(10,2);
    v_anulada timestamptz;
BEGIN
    IF NEW.anulado_en IS NOT NULL OR restaurando() THEN RETURN NEW; END IF;   -- anular un recibo siempre se permite
    SELECT anulado_en INTO v_anulada FROM ordenes WHERE id = NEW.orden_id FOR UPDATE;
    IF v_anulada IS NOT NULL THEN
        RAISE EXCEPTION 'La orden % está anulada', NEW.orden_id;
    END IF;
    SELECT total - pagado
           + CASE WHEN TG_OP = 'UPDATE' AND OLD.orden_id = NEW.orden_id AND OLD.anulado_en IS NULL THEN OLD.monto ELSE 0 END
      INTO v_saldo FROM resumen_ordenes WHERE id = NEW.orden_id;
    IF NEW.monto > v_saldo THEN
        RAISE EXCEPTION 'El pago de % pasa el saldo de la orden % (saldo: %)', NEW.monto, NEW.orden_id, v_saldo;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER no_pagar_de_mas BEFORE INSERT OR UPDATE OF monto, orden_id, anulado_en ON recibos
    FOR EACH ROW EXECUTE FUNCTION revisar_pago();

-- 6) Inventario: una salida no puede dejar la bodega en negativo, y la salida de una solicitud
--    debe ser de una solicitud aprobada, del mismo material y por la misma cantidad.
CREATE FUNCTION revisar_movimiento() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_sol solicitudes%ROWTYPE;
    v_hay int;
BEGIN
    IF restaurando() THEN RETURN NEW; END IF;
    PERFORM 1 FROM materiales WHERE id = NEW.material_id FOR UPDATE;   -- evita dos salidas a la vez
    IF NEW.solicitud_id IS NOT NULL THEN                              -- el material ya lo cuida la FK compuesta
        SELECT * INTO v_sol FROM solicitudes WHERE id = NEW.solicitud_id;
        IF v_sol.estado <> 'aprobada' OR v_sol.cantidad <> NEW.cantidad THEN
            RAISE EXCEPTION 'La solicitud % no está aprobada o la cantidad no coincide', NEW.solicitud_id;
        END IF;
    END IF;
    IF NEW.tipo = 'salida' OR NEW.cantidad < 0 THEN
        SELECT cantidad INTO v_hay FROM existencias WHERE id = NEW.material_id;
        IF v_hay - abs(NEW.cantidad) < 0 THEN
            RAISE EXCEPTION 'No hay suficiente material % (hay %, se piden %)', NEW.material_id, v_hay, abs(NEW.cantidad);
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER revisar_inventario BEFORE INSERT ON movimientos_inventario
    FOR EACH ROW EXECUTE FUNCTION revisar_movimiento();

-- 7) Los movimientos de inventario no se editan ni se borran: un error se corrige con un movimiento de ajuste.
--    Así el historial no se puede alterar y la revisión de existencias (solo en INSERT) no se puede saltar.
CREATE FUNCTION movimiento_inmutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Los movimientos de inventario no se modifican; registre un ajuste';
END $$;

CREATE TRIGGER movimientos_sin_cambios BEFORE UPDATE OR DELETE ON movimientos_inventario
    FOR EACH ROW EXECUTE FUNCTION movimiento_inmutable();

-- ============ Permisos por rol (lo que cada uno puede ver y hacer) ============
-- La facultad ve TODO. El profesor ve lo de sus clases y el estudiante lo suyo; eso lo filtra el programa al iniciar
-- sesión según cuentas.rol. Si se quisiera reforzar en la base de datos, se puede hacer con roles de PostgreSQL, por ejemplo:
--   CREATE ROLE app_facultad;  GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_facultad;
-- y con Row Level Security (CREATE POLICY …) para que cada estudiante y profesor vea solo sus filas.

-- ============ EXTENSIONES (no están en el diagrama v14) ============
-- Conservan funciones que el sistema ya tenía. Cada una dice para qué es.

-- Bloqueo de la cuenta tras 5 contraseñas equivocadas seguidas
ALTER TABLE cuentas ADD COLUMN fallos int NOT NULL DEFAULT 0,
                    ADD COLUMN bloqueada_hasta timestamptz;
-- Ciudad aproximada desde donde se hizo cada acción (la IP no se guarda)
ALTER TABLE bitacora ADD COLUMN lugar text NOT NULL DEFAULT '';
-- Razón de la consulta, consentimiento informado firmado (Ley 787), rechazo del profesor con su observación,
-- revisión del odontograma, alta y eliminación del paciente (se oculta y la Facultad lo puede restaurar)
ALTER TABLE pacientes
    ADD COLUMN razon              text NOT NULL DEFAULT '',
    ADD COLUMN firma              text NOT NULL DEFAULT '',      -- imagen PNG de la firma
    ADD COLUMN firmado_en         timestamptz,
    ADD COLUMN rechazo            text NOT NULL DEFAULT '',
    ADD COLUMN rechazado_por_id   int REFERENCES cuentas (id) ON DELETE RESTRICT,
    ADD COLUMN rechazado_en       timestamptz,
    ADD COLUMN odonto_revision    text NOT NULL DEFAULT '' CHECK (odonto_revision IN ('', 'pedida', 'aprobada', 'devuelta')),
    ADD COLUMN odonto_revisor_id  int REFERENCES cuentas (id) ON DELETE RESTRICT,
    ADD COLUMN odonto_nota        text NOT NULL DEFAULT '',
    ADD COLUMN finalizado_en      timestamptz,
    ADD COLUMN finalizado_por_id  int REFERENCES cuentas (id) ON DELETE RESTRICT,
    ADD COLUMN finalizado_resumen text NOT NULL DEFAULT '',
    ADD COLUMN eliminado_en       timestamptz,
    ADD COLUMN eliminado_por_id   int REFERENCES cuentas (id) ON DELETE RESTRICT,
    ADD COLUMN eliminado_motivo   text NOT NULL DEFAULT '';
-- El resto de la historia clínica (RF05) y quién la actualizó
ALTER TABLE historias
    ADD COLUMN direccion                  text NOT NULL DEFAULT '',
    ADD COLUMN emergencia_nombre          text NOT NULL DEFAULT '',
    ADD COLUMN emergencia_telefono        text NOT NULL DEFAULT '',
    ADD COLUMN antecedentes               text NOT NULL DEFAULT '',
    ADD COLUMN medicamentos               text NOT NULL DEFAULT '',
    ADD COLUMN antecedentes_odontologicos text NOT NULL DEFAULT '',
    ADD COLUMN examen_extraoral           text NOT NULL DEFAULT '',
    ADD COLUMN examen_intraoral           text NOT NULL DEFAULT '',
    ADD COLUMN actualizada_por_id         int REFERENCES cuentas (id) ON DELETE SET NULL;
-- Nota del hallazgo en cada diente
ALTER TABLE dientes ADD COLUMN nota text NOT NULL DEFAULT '';
-- Sala del edificio de cada clínica (1 pregrado, 2 posgrado), para dibujar el plano
ALTER TABLE salas ADD COLUMN en_sala smallint NOT NULL DEFAULT 1 CHECK (en_sala IN (1, 2));
-- Razón de la cita, motivo si se cancela, indicaciones al terminarla y recordatorio por WhatsApp (RF11, RF12)
ALTER TABLE citas
    ADD COLUMN razon              text NOT NULL DEFAULT '',
    ADD COLUMN motivo_cancelacion text NOT NULL DEFAULT '',
    ADD COLUMN indicaciones       text NOT NULL DEFAULT '',
    ADD COLUMN recordada_en       timestamptz;
-- Una clínica tiene a lo sumo una falla abierta
CREATE UNIQUE INDEX una_falla_abierta ON reportes (sala_id) WHERE resuelto_en IS NULL;
-- Área clínica de cada procedimiento (RF15)
ALTER TABLE tarifario ADD COLUMN area text NOT NULL DEFAULT '';
-- Foto del recibo y el estudiante en cuya hoja de control va
ALTER TABLE recibos
    ADD COLUMN url           text,
    ADD COLUMN estudiante_id int REFERENCES cuentas (id) ON DELETE RESTRICT;
-- Para qué fecha pide el estudiante el material, nota si se rechaza y quién la atendió (RF17)
ALTER TABLE solicitudes
    ADD COLUMN para            date,
    ADD COLUMN nota            text NOT NULL DEFAULT '',
    ADD COLUMN atendida_por_id int REFERENCES cuentas (id) ON DELETE RESTRICT;

-- Cambio de estudiante a cargo, con el resumen de lo que trabajó el anterior
CREATE TABLE traspasos (
    id            serial PRIMARY KEY,
    paciente_id   int NOT NULL REFERENCES pacientes (id) ON DELETE CASCADE,
    de_id         int REFERENCES cuentas (id) ON DELETE RESTRICT,
    a_id          int NOT NULL REFERENCES cuentas (id) ON DELETE RESTRICT,
    resumen       text NOT NULL,
    hecho_por_id  int REFERENCES cuentas (id) ON DELETE RESTRICT,
    creado_en     timestamptz NOT NULL DEFAULT now()
);
-- Pacientes que pidieron por WhatsApp que su estudiante les contacte (RF12)
CREATE TABLE pedidos_contacto (
    id              serial PRIMARY KEY,
    paciente_id     int NOT NULL REFERENCES pacientes (id) ON DELETE CASCADE,
    pedido_en       timestamptz NOT NULL DEFAULT now(),
    atendido_en     timestamptz,
    atendido_por_id int REFERENCES cuentas (id) ON DELETE RESTRICT
);
CREATE INDEX ON traspasos (paciente_id);
CREATE INDEX ON pedidos_contacto (paciente_id);
