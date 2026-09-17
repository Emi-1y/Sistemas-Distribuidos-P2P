# Una sola imagen para los tres peers y para el cliente.
#
# Los peers son simetricos —esa es la propiedad central del sistema— y tres
# Dockerfiles la dejarian de garantizar por construccion: bastaria que uno se
# quedara sin actualizar para tener un anillo con un peer que se comporta
# distinto y no saber por que. Lo unico que cambia entre los tres son variables
# de entorno, que es lo que la SPEC-02 decidio cuando saco la configuracion del
# codigo.

FROM python:3.13-slim

# La misma version que corrio la suite en el anfitrion: estrenar interprete
# dentro del contenedor seria cambiar dos cosas a la vez.

WORKDIR /app

# Sin buffer, los logs de uvicorn salen cuando pasan y no cuando el proceso
# decide vaciar: en una demo que se mira en directo, eso importa.
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ server/
COPY client/ client/
COPY demo/ demo/

# `tests/` entra en la imagen a proposito: correr la suite dentro es un
# criterio de aceptacion (AC-04), no un extra. Es la unica forma de saber que
# el arreglo de la jaula de la SPEC-05 vale tambien en Linux.
COPY tests/ tests/

# Y con ella el compose, que es lo que lee uno de esos tests. Sin el, la suite
# de dentro tendria menos tests que la de fuera, y «pasa completa» dejaria de
# querer decir lo mismo en los dos sitios.
COPY docker-compose.yml .

# Sin CMD: cada servicio pone el suyo en el compose, porque el puerto es lo
# unico que cambia y verlo al lado de la identidad del peer evita el clasico
# peer3 escuchando en el puerto de peer2.
