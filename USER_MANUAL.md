# Manual de Usuario - Cambio de Dia de Facturacion

## Portada
- Modulo: contract_management
- Version: 17.0.x (actualizable)
- Ultima actualizacion: enero 2026
- Alcance: Cambio de dia de facturacion en suscripciones/contratos
- Publico: Agentes de cobranza y atencion

![Portada - Cambio de dia de facturacion](../static/description/cambio-dia-portada.png "Reemplazar con captura de la pantalla principal del wizard")

## Seccion: Cambiar dia de facturacion

**Objetivo**
- Ajustar el dia de cobro/proxima factura alineado a la capacidad de pago, con control de abusos y trazabilidad.

**Disponibilidad**
- Visible si el cliente esta al dia (sin facturas vencidas ni en dunning) y con metodo de pago valido.
- Una solicitud por ciclo y enfriamiento de 30 dias entre cambios.
- Oculto cuando existan facturas vencidas, cobranza activa o pagos pendientes.

**Flujo para agentes**
1. Abrir la suscripcion/contrato y pulsar "Cambiar dia de facturacion".
2. Revisar el Dia actual / Proxima fecha de factura.
3. Elegir el nuevo dia (o fecha de proxima factura) permitido.
4. Verificar el Total prorrateado (a facturar) que incluye el tramo parcial.
5. Confirmar. El sistema valida elegibilidad, registra el cambio, genera el prorrateo y actualiza la proxima fecha.

**Que valida el sistema**
- Sin facturas vencidas ni en dunning.
- Sin pagos pendientes relevantes al ciclo.
- No se ha hecho otro cambio en el mismo ciclo y han pasado >=30 dias desde el ultimo.
- El nuevo dia no retrocede facturacion ya emitida.

**Resultados del cambio**
- Se actualiza la proxima fecha de factura.
- Se calcula un prorrateo para el tramo parcial hasta el nuevo dia.
- Factura de periodo puente (stub): se crea una factura parcial que cubre desde el fin del ciclo actual hasta el inicio del nuevo ciclo, usando el monto prorrateado para evitar dias sin cubrir o doble cobro.
- Se registra en el Historial de cambios de dia:
  - Fecha anterior y nueva de proxima factura.
  - Monto prorrateado y factura de periodo puente ligada.
  - Usuario y timestamp para auditoria.

**Mensajes y bloqueos comunes**
- "Cliente con facturas vencidas": regularizar antes de reintentar.
- "Limite de cambios alcanzado en este ciclo": esperar al siguiente ciclo.
- "Debe esperar 30 dias desde el ultimo cambio": reintentar al cumplir el enfriamiento.

**Buenas practicas**
- Explicar al cliente el prorrateo y la factura de periodo puente antes de confirmar.
- Evitar cambios consecutivos; usarlos solo por necesidad justificada.
- Anotar motivo en notas internas si es excepcion.

**Auditoria**
- Historial visible en la ficha de la suscripcion bajo "Cambios de dia de pago".
- Solo lectura; no se pueden borrar entradas.
