# Recordatorios automáticos por WhatsApp (RF12)

El día antes de cada cita programada, el sistema le envía al paciente un recordatorio por WhatsApp, entre las
8:00 a. m. y las 6:00 p. m. Usa la API oficial de WhatsApp (Cloud API de Meta). Una máquina de Fly
(`recordatorios`) corre cada hora `flask --app prototipo recordar`, que:

- toma las citas de mañana en estado Programada, de pacientes con teléfono que no fueron dados de alta ni eliminados;
- envía la plantilla con el nombre del paciente, la fecha, la hora y la clínica;
- marca la cita como recordada y lo anota en la bitácora. En Inicio y en Citas se ve "Recordatorio enviado".

Si un envío falla, lo intenta de nuevo la hora siguiente. Si la cita se reprograma, se vuelve a recordar para la fecha
nueva. El botón "Recordar por WhatsApp", para enviarlo a mano, sigue disponible.

## Prueba gratis (número de prueba de Meta)
El número de prueba envía gratis, pero solo a **5 teléfonos** registrados a mano. Sirve para probar con los
teléfonos del equipo haciendo de pacientes.

1. Entrar a https://developers.facebook.com con una cuenta de Facebook → *Mis apps* → *Crear app* → tipo
   *Negocios* → agregar el producto **WhatsApp**. Meta crea un número de prueba.
2. En *WhatsApp → Configuración de la API*:
   - copiar el **ID del número de teléfono**;
   - en *Para*, agregar hasta 5 teléfonos del equipo y confirmarlos con el código que llega por WhatsApp.
3. En *WhatsApp Manager → Plantillas de mensajes*, crear una plantilla:
   - Nombre: `recordatorio_cita` · Categoría: **Utilidad** · Idioma: **Español**.
   - Cuerpo: `Hola, {{1}}. Le recordamos su cita en las Clínicas Odontológicas de la UAM el {{2}} a las {{3}} en {{4}}. Si no puede asistir, avísele a su estudiante.`
   - Esperar a que la aprueben; suele tardar minutos.
4. Crear un token que no venza; el de la página de inicio vence en 24 horas. En *business.facebook.com →
   Configuración del negocio → Usuarios del sistema*:
   - crear uno con rol de administrador;
   - asignarle la app y la cuenta de WhatsApp;
   - generar un token con los permisos `whatsapp_business_messaging` y `whatsapp_business_management`.
5. Guardar los datos en Fly:
   ```
   fly secrets set -a clinicas-odontologicas-uam WHATSAPP_TOKEN=<token> WHATSAPP_NUMERO_ID=<id del número> WHATSAPP_PLANTILLA=recordatorio_cita
   ```
6. Para probar: crear una cita para mañana a un paciente cuyo teléfono sea uno de los 5 registrados. Esperar a la
   hora siguiente, o correrlo en el momento:
   `fly ssh console -a clinicas-odontologicas-uam -C "flask --app prototipo recordar"`.

Con el número de prueba, a un teléfono que no está entre los 5 registrados el envío falla (error 131030). El sistema
lo deja anotado en el registro de la máquina y sigue con los demás.

## Pasar a uso real con pacientes
- **Número propio:** un chip que no se use en WhatsApp normal (unos US$2 a 5), agregado en *WhatsApp Manager →
  Números de teléfono*. Las plantillas se vuelven a crear en esa cuenta.
- **Método de pago:** una tarjeta en la cuenta de WhatsApp de Meta.
- **Verificar a la UAM como negocio** en *Configuración del negocio → Centro de seguridad*, para pasar del límite
  inicial de envíos por día.
- **Costo:** cada recordatorio es un mensaje de utilidad. Nicaragua está en el grupo "Resto de Latinoamérica", con
  unos **US$0.011 a 0.013 por mensaje entregado**. Por ejemplo, 870 recordatorios al mes son unos US$10 a 12. Las
  tarifas cambian por fechas: confirmar en la tabla oficial,
  https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing.
- **Ley 787:** el consentimiento informado ya autoriza el uso de los datos del paciente para su atención. El
  recordatorio solo lleva nombre, fecha, hora y clínica, sin datos clínicos.

No se recomiendan los bots no oficiales que manejan un WhatsApp normal desde una computadora: van contra las
reglas de WhatsApp y el número puede ser bloqueado.
