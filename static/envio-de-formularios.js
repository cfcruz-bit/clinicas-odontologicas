// Al enviar un formulario, el botón que se tocó muestra que está cargando y el formulario no se envía dos veces.
// El botón no se desactiva: si tiene name/value (por ejemplo accion=aprobar) el navegador dejaría de mandarlo.
document.addEventListener("submit", e => {
  const f = e.target;
  if (e.defaultPrevented) return;
  if (f.getAttribute("aria-busy") === "true") return e.preventDefault();
  f.setAttribute("aria-busy", "true");
  (e.submitter || f.querySelector("button:not([type=button])"))?.classList.add("cargando");
});
// Al volver con Atrás el navegador puede mostrar la página tal como quedó, todavía cargando
addEventListener("pageshow", e => {
  if (!e.persisted) return;
  document.querySelectorAll("form[aria-busy]").forEach(f => f.removeAttribute("aria-busy"));
  document.querySelectorAll(".cargando").forEach(b => b.classList.remove("cargando"));
});
