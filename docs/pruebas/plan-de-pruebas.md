# Plan de pruebas de usabilidad, rendimiento, portabilidad y disponibilidad (RNF01 a RNF04)

## RNF01 - Usabilidad
- 5 estudiantes y 2 profesores que no conocen el sistema, cada uno con la guía de 15 minutos.
- Tareas: registrar un paciente, llenar la historia clínica, agendar una cita, subir un recibo, aprobar un paciente
  y firmar un recibo.
- Se mide: si termina cada tarea sin ayuda, cuánto tarda y dónde se equivoca.
- Al final responden el cuestionario SUS (System Usability Scale), en `docs/pruebas/cuestionario-sus.md`.
- Aprobado si el 80 % de las tareas se terminan sin ayuda y el puntaje SUS es de 68 o más.

## RNF02 - Rendimiento
- Objetivo: cada página responde en menos de 3 segundos con 50 usuarios a la vez.
- Herramienta: `pruebas/carga.py`, siempre contra una rama de pruebas de Neon, nunca la base real.
- Escenario: 40 estudiantes (inicio, pacientes, expediente, historia clínica, guardar la historia, citas,
  odontograma, tratamientos, clínicas) y 10 profesores (inicio, pacientes, citas, hoja de control, clínicas).
  Cada uno hace clic cada 1 a 3 segundos.
- Se corre en una máquina de Fly igual a la del sistema (1 CPU compartida, 512 MB) y en la misma región que la base.
- Aprobado si el percentil 95 es menor de 3 s y no hay errores.

### Resultados
| Fecha | Duración | Visitas | Promedio | 95 % | Máximo | Errores | Resultado |
|---|---|---|---|---|---|---|---|
| 16/11/2026 | 5 min, 50 usuarios | 7392 | 0,03 s | 0,05 s | 1,04 s (entrar) | 0 | Aprobado |

Los tiempos son del servidor, medidos dentro de la red de Fly. Desde Nicaragua se suma el viaje por internet, unos 0,1 a 0,2 s por página.

## RNF03 - Portabilidad
Probar las mismas tareas de RNF01 en:

| Equipo | Navegador |
|---|---|
| Computadora de la clínica (Windows) | Chrome, Edge, Firefox |
| Mac | Safari |
| Celular Android | Chrome |
| iPhone | Safari |

Revisar en cada uno: que el menú y las tablas se vean en pantalla chica, que la cámara abra al subir fotos, que el PDF
descargue y que el QR de registro se pueda escanear.

## RNF04 - Disponibilidad
- Meta: 99 % del horario de turnos, de lunes a sábado de 7:00 a. m. a 6:00 p. m. (hora de Managua).
- Medición: un monitor externo gratuito (por ejemplo UptimeRobot) visita la página de entrada cada 5 minutos y
  avisa por correo si no responde. Al final de cada mes se anota el porcentaje en horario de turnos.
- Ventana de mantenimiento: las publicaciones y cambios de la base se hacen fuera del horario de turnos, de lunes a
  viernes después de las 6:00 p. m. o los domingos. El respaldo diario corre una vez al día y no detiene el sistema.
- Si la página no está en uso, Fly apaga la máquina y la vuelve a encender con la primera visita, que tarda unos
  segundos más. Eso no cuenta como caída.

### Resultados
| Mes | Disponibilidad en horario de turnos | Caídas | Causa |
|---|---|---|---|

## Registro de resultados
Anotar fecha, quién probó, equipo, resultado y fallas encontradas en una hoja compartida; cada falla se abre como
issue en GitHub.
