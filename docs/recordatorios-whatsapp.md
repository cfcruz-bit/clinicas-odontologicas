# Recordatorios automáticos por WhatsApp (RF12)

El día antes de cada cita programada, el sistema le envía al paciente un recordatorio por WhatsApp, entre las
8:00 a. m. y las 6:00 p. m. Usa la API oficial de WhatsApp (Cloud API de Meta). Una máquina de Fly
(`recordatorios`) corre cada hora `flask --app prototipo recordar`, que:

- toma las citas de mañana en estado Programada, de pacientes con teléfono que no fueron dados de alta ni eliminados;
- envía la plantilla con el nombre del paciente, la fecha, la hora y la clínica;
- marca la cita como recordada y lo anota en la bitácora. En Inicio y en Citas se ve "Recordatorio enviado".

El mensaje trae dos botones. Cuando el paciente toca uno, Meta avisa al sistema en
`https://clinicas-odontologicas-uam.fly.dev/whatsapp`:

- **Sí, asistiré:** la cita pasa a **Confirmada** y el paciente recibe "Gracias. Su cita del … quedó confirmada."
- **No podré asistir:** la cita pasa a **Cancelada** con el motivo "El paciente avisó por WhatsApp que no podrá
  asistir", la clínica queda libre y el paciente recibe un aviso de que su estudiante le contactará.
- Solo se acepta el botón si viene firmado por Meta, si llega desde el mismo teléfono del paciente y si la cita
  sigue Programada.
- La respuesta queda en la bitácora.

## El bot: cuando el paciente escribe
Si el paciente le escribe cualquier cosa al número de la clínica ("hola", "¿a qué hora es mi cita?", un audio),
el bot le contesta por su nombre con un menú:

```
1. Ver mis próximas citas
2. Confirmar mi próxima cita
3. No podré asistir a mi próxima cita
4. Ubicación y horario
5. Que mi estudiante me contacte
```

- **1:** hasta 5 citas programadas o confirmadas, de hoy en adelante, con fecha, hora, clínica y estado.
- **2:** confirma la próxima cita. Si ya estaba confirmada, solo se lo dice.
- **3:** antes de cancelar pregunta con dos botones, *Sí, cancelarla* y *No, mantenerla*. Con el primero la cita
  pasa a Cancelada con el mismo motivo que el botón del recordatorio, aunque ya estuviera confirmada.
- **4:** el texto de `CLINICA_UBICACION`, o uno general si no está configurado.
- **5:** al estudiante le aparece en Inicio "Pacientes que pidieron que les contacte", con un botón para
  escribirle y otro para marcar "Ya le contacté". La pestaña muestra *Inicio (1)* mientras haya pendientes.
  Pedirlo varias veces no repite el aviso.
- El bot **no agenda citas nuevas**. Las agenda el estudiante, porque tiene que respetar sus turnos y las clínicas
  libres. Si el paciente no tiene citas, el bot le ofrece la opción 5.
- Reconoce al paciente por su teléfono. Si varios pacientes tienen el mismo número (una madre y sus hijos), les
  muestra las citas de todos con el nombre de cada uno. A un número que no es de ningún paciente le dice que pida a
  su estudiante que lo anote.
- Lo que cambia queda en la bitácora. Estas respuestas van dentro de las 24 horas en que escribió el paciente y no
  usan plantilla, así que entran en las conversaciones gratis de Meta.

Para cambiar el texto de ubicación y horario:
`fly secrets set -a clinicas-odontologicas-uam CLINICA_UBICACION="Clínicas Odontológicas UAM, <dirección>. Lunes a sábado..."`

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
   - Cuerpo: `Hola, {{1}}. Le recordamos su cita en las Clínicas Odontológicas de la UAM el {{2}} a las {{3}} en {{4}}. ¿Asistirá?`
   - Botones: tipo **Respuesta rápida**, dos botones en este orden: `Sí, asistiré` y `No podré asistir`.
   - Esperar a que la aprueben; suele tardar minutos.
4. Crear un token que no venza; el de la página de inicio vence en 24 horas. En *business.facebook.com →
   Configuración del negocio → Usuarios del sistema*:
   - crear uno con rol de administrador;
   - asignarle la app y la cuenta de WhatsApp;
   - generar un token con los permisos `whatsapp_business_messaging` y `whatsapp_business_management`.
5. Configurar las respuestas, en *WhatsApp → Configuración* de la app:
   - URL de devolución de llamada: `https://clinicas-odontologicas-uam.fly.dev/whatsapp`.
   - Token de verificación: una palabra secreta inventada por ustedes, la misma de `WHATSAPP_VERIFICAR` en el paso 6.
   - Suscribirse al campo **messages**.
   - En *Configuración de la app → Básica*, copiar la **Clave secreta de la app**.
6. Guardar los datos en Fly, antes del paso 5 para que la verificación funcione:
   ```
   fly secrets set -a clinicas-odontologicas-uam WHATSAPP_TOKEN=<token> WHATSAPP_NUMERO_ID=<id del número> WHATSAPP_PLANTILLA=recordatorio_cita WHATSAPP_VERIFICAR=<palabra secreta> WHATSAPP_SECRETO=<clave secreta de la app>
   ```
7. Para probar: crear una cita para mañana a un paciente cuyo teléfono sea uno de los 5 registrados. Esperar a la
   hora siguiente, o correrlo en el momento:
   `fly ssh console -a clinicas-odontologicas-uam -C "flask --app prototipo recordar"`. Después tocar uno de los botones
   en el teléfono y revisar que la cita cambie de estado en Citas. Para el bot, escribir "hola" al número de prueba
   desde uno de los 5 teléfonos y probar las opciones del menú.

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
  La respuesta de "Gracias…" va dentro de las 24 horas en que escribió el paciente; Meta da 1,000 de esas gratis al
  mes por número, así que en la práctica no suma costo.
- **Ley 787:** el consentimiento informado ya autoriza el uso de los datos del paciente para su atención. El
  recordatorio solo lleva nombre, fecha, hora y clínica, sin datos clínicos.

No se recomiendan los bots no oficiales que manejan un WhatsApp normal desde una computadora: van contra las
reglas de WhatsApp y el número puede ser bloqueado.
