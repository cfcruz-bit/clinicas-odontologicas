# Cumplimiento de la Ley N.º 787, Ley de Protección de Datos Personales (RNF06)

Ley publicada en La Gaceta N.º 61 del 29 de marzo de 2012; su reglamento es el Decreto 36-2012 (La Gaceta N.º 200
del 19 de octubre de 2012). La ley cuenta los datos de **salud** entre los datos **sensibles**, y para esos el
reglamento pide **consentimiento expreso** del titular.

Este documento explica cómo el sistema cumple cada punto. No es una opinión legal: la Facultad debe confirmarlo con
la asesoría legal de la UAM.

| Lo que pide la ley | Cómo lo cumple el sistema |
|---|---|
| Consentimiento expreso, por escrito o por medio electrónico, para datos de salud | El consentimiento informado de cada paciente incluye la autorización para guardar sus datos personales y de salud, y el paciente la firma en la tableta o el celular. El expediente muestra si falta la firma. |
| Finalidad: los datos se usan solo para lo que se autorizó | El consentimiento dice para qué se usan: la atención del paciente y la enseñanza supervisada. El sistema no comparte datos con otros sistemas ni los vende. |
| Confidencialidad: acceso solo de quien lo necesita | El estudiante ve solo a sus pacientes. El profesor ve solo a los pacientes de sus estudiantes. La Facultad ve todo para administrar. Las fotos y radiografías están en un almacén privado y se abren con un enlace que vence en 5 minutos, después de revisar quién las pide. |
| Seguridad de los datos | Contraseñas cifradas, conexión segura (HTTPS), bloqueo de la cuenta tras 5 contraseñas equivocadas, cierre de sesión a los 15 minutos sin uso y cuentas que la Facultad puede desactivar. |
| Calidad: datos exactos y al día | Se revisan la cédula, el teléfono, el correo y las fechas al escribirlos. El estudiante corrige los datos y la historia clínica, y cada cambio queda anotado. |
| Derecho de acceso | La Facultad puede imprimir el expediente completo en PDF y entregárselo al paciente que lo pida. |
| Derecho de rectificación | El estudiante a cargo corrige los datos del paciente y su historia, y cada cambio queda en la bitácora. Después del alta el expediente queda cerrado; ver el pendiente abajo. |
| Derecho de cancelación | La Facultad elimina al paciente escribiendo su cédula y el motivo. Queda marcado como eliminado y deja de verse. No se borra del todo porque el expediente clínico se debe conservar; ver el pendiente abajo. |
| Registro de quién hizo qué | La bitácora guarda quién, cuándo, qué y desde qué ciudad. No se puede editar ni borrar. |
| Respaldo | Ver [respaldos.md](respaldos.md). |

## Pendiente para la Facultad
- Nombrar a la persona responsable de los datos en la Facultad, que es quien recibe las solicitudes de los pacientes.
- Definir cuántos años se conserva el expediente de un paciente dado de alta o eliminado, según las normas de
  expedientes clínicos del MINSA, y borrar los datos al cumplirse ese plazo.
- Si un paciente dado de alta pide corregir un dato, hoy no hay pantalla para hacerlo: la Facultad lo anota en el
  expediente en papel. Si pasa seguido, se agrega la corrección para la Facultad.
- La bitácora calcula la ciudad aproximada de cada ingreso consultando la IP en un servicio externo (ipwho.is), que
  recibe solo la IP y no datos de pacientes. Si la asesoría legal lo pide, se puede quitar.
