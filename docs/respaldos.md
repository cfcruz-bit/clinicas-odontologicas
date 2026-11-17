# Respaldos de la base de datos (RNF07)

La base está en Neon. Neon guarda el historial de cambios y permite volver a cualquier momento dentro de la ventana de restauración del plan (en el plan gratuito, 1 día; en los planes de pago se puede ampliar).

## Además, un respaldo propio cada semana
1. Instalar las herramientas de PostgreSQL (`pg_dump`, versión 16 o más nueva).
2. Copiar la cadena de conexión de Neon (Dashboard → Connect). No se guarda en el repositorio.
3. Ejecutar:
   ```
   pg_dump "<cadena de Neon>" --format=custom --file=clinicas-AAAA-MM-DD.dump
   ```
4. Guardar el archivo en el Drive de la Facultad. Conservar al menos los 4 últimos.

Las fotos y recibos no están dentro de la base (solo su enlace): el almacenamiento S3 debe tener activado el versionado o su propia copia.

## Restaurar
1. En Neon, crear una rama nueva desde el momento anterior al problema (Branches → Create branch → Point in time), o
2. Restaurar el respaldo propio en una rama vacía:
   ```
   pg_restore --no-owner --dbname "<cadena de la rama>" clinicas-AAAA-MM-DD.dump
   ```
3. Revisar los datos y, si están bien, apuntar `DATABASE_URL` de Fly.io a esa rama.

Probar una restauración al menos una vez por semestre.

## Ya en el sistema
- Los pacientes eliminados no se borran: quedan marcados y la Facultad los puede restaurar.
- Las citas canceladas y los cambios de precio quedan como historial.
