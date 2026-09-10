# SPEC-06 — Despliegue y demo

> **Sin redactar.** Este archivo existe solo para que no se pierdan las notas
> que van saliendo mientras se cierran las SPECs anteriores. La SPEC-06 se
> escribe con la plantilla completa de diez secciones cuando la SPEC-05 esté
> verde y mergeada.

---

## Notas acumuladas

### El guion de demo necesita un archivo con bastantes bloques

Salió al verificar la SPEC-02 con tres peers reales. Las claves `5f3e:0`,
`5f3e:1` y `5f3e:2` cayeron en `peer1`, `peer2` y `peer1`: **tres bloques no
garantizan tocar los tres peers**.

Es el comportamiento correcto —la colocación es un hash, no un reparto por
turnos— pero un archivo de tres bloques puede dejar un peer vacío y la demo
parecería que no reparte. Con 4 MiB de bloque, un archivo de 12 MiB da tres
bloques; hace falta bastante más.

Al escribir la SPEC-06:

1. Elegir el tamaño del archivo de demostración **calculando antes** en cuántos
   peers caen sus bloques, con `choose_nodes`, en vez de confiar en que salga
   repartido.
2. Que el guion muestre el reparto explícitamente: para cada bloque, en qué peer
   vive, listando el contenido de `storage/<file_id>/` en los tres.
3. Dejar escrito en el propio guion por qué el reparto no es exactamente un
   tercio en cada peer: es un hash, y con pocas claves la varianza se ve.
