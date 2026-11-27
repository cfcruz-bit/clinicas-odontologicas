# Respaldos de la base de datos (RNF07)

## Qué hay
1. **Respaldo diario propio.** Una vez al día una máquina de Fly (`respaldo-diario`) corre
   `flask --app prototipo respaldar`. Copia todas las tablas en un archivo `.zip` y lo guarda en el almacén privado
   de las fotos, en la carpeta `respaldos/`. Se guardan los últimos 30.
2. **Volver a un momento de las últimas 6 horas.** Neon lo permite en el plan gratuito. Sirve, por ejemplo, si
   alguien borró algo por error esta mañana.
3. **Las fotos, radiografías y recibos** ya están en el almacén y la base guarda solo su enlace. El sistema nunca
   cambia una foto: solo agrega o borra.

Neon no programa copias propias en el plan gratuito. Por eso existe el respaldo diario propio.

## Restaurar
Siempre en una rama nueva de Neon, nunca encima de la base real: el comando se niega si la base ya tiene datos.

**Desde el respaldo diario:**
1. Ver los respaldos guardados:
   `fly ssh console -a clinicas-odontologicas-uam -C "python -c \"import prototipo as m; print([o['Key'] for o in m.s3().list_objects_v2(Bucket=m.ALMACEN['bucket'], Prefix='respaldos/')['Contents']])\""`
2. Crear una rama vacía en Neon y copiar su cadena de conexión:
   `neonctl branches create --project-id weathered-salad-12660737 --name restaurada --parent main --schema-only`
   (o en la consola de Neon: Branches → Create branch).
3. Cargar el respaldo en esa rama:
   `fly ssh console -a clinicas-odontologicas-uam -C "env DATABASE_URL='<cadena de la rama nueva>' flask --app prototipo restaurar respaldos/AAAA-MM-DD_HHMM.zip"`
4. Revisar los datos de la rama. Si están bien, apuntar el sistema a ella:
   `fly secrets set -a clinicas-odontologicas-uam DATABASE_URL='<cadena de la rama nueva>'`

**A un momento de las últimas 6 horas:** en la consola de Neon, Branches → main → Restore, y elegir la fecha y la
hora. Neon guarda el estado anterior por si hay que deshacerlo.

## Prueba de restauración
- Las pruebas automáticas (`pruebas/sistema.py`) sacan un respaldo, vacían la base, lo restauran y comprueban que vuelvan
  todas las filas de todas las tablas y que los números automáticos sigan sin chocar.
- Hacer además una restauración de verdad, como arriba, al menos una vez por semestre, y anotar la fecha aquí.

| Fecha | Quién | Respaldo | Resultado |
|---|---|---|---|
| 16/11/2026 | Equipo de desarrollo | respaldos/2026-11-16_2008.zip | Restaurado en una rama vacía: las 20 tablas con las mismas filas que la base real |

## Ya en el sistema
- Los pacientes eliminados no se borran: quedan marcados y la Facultad los puede restaurar.
- Las citas canceladas y los cambios de precio quedan como historial.
