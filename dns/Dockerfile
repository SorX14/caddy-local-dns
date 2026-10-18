FROM alpine:3
RUN apk add --no-cache dnsmasq python3
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY dnsmasq.conf /etc/dnsmasq.conf
COPY sync_resolvers.py diagnostics.py web.py ./
COPY static ./static
CMD ["dnsmasq", "--keep-in-foreground"]
