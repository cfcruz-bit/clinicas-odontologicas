# Plan de pruebas de rendimiento, usabilidad y compatibilidad (RNF01, RNF02, RNF04)

## RNF01 - Rendimiento
- Objetivo: cada página responde en menos de 3 segundos con 50 usuarios a la vez.
- Herramienta: [Locust](https://locust.io) contra una rama de pruebas de Neon (nunca la base real).
- Escenario: 40 estudiantes (entrar, ver pacientes, agendar cita, subir foto) y 10 profesores (aprobar, firmar recibos), durante 10 minutos.
- Se anota: tiempo medio y percentil 95 por página, errores, uso de CPU y memoria en Fly.io.
- Aprobado si el percentil 95 es menor de 3 s y no hay errores.

## RNF02 - Usabilidad
- 5 estudiantes y 2 profesores que no conocen el sistema, cada uno con la guía de 15 minutos.
- Tareas: registrar un paciente, agendar una cita, subir un recibo, aprobar un paciente, firmar un recibo.
- Se mide: si termina cada tarea sin ayuda, cuánto tarda y dónde se equivoca.
- Al final responden el cuestionario SUS (System Usability Scale).
- Aprobado si el 80 % de las tareas se terminan sin ayuda y el puntaje SUS es de 68 o más.

## RNF04 - Compatibilidad
Probar las mismas tareas en:

| Equipo | Navegador |
|---|---|
| Computadora de la clínica (Windows) | Chrome, Edge, Firefox |
| Mac | Safari |
| Celular Android | Chrome |
| iPhone | Safari |

Revisar en cada uno: que el menú y las tablas se vean en pantalla chica, que la cámara abra al subir fotos, que el PDF descargue y que el QR de registro se pueda escanear.

## Registro de resultados
Anotar fecha, quién probó, equipo, resultado y fallas encontradas en una hoja compartida; cada falla se abre como issue en GitHub.
