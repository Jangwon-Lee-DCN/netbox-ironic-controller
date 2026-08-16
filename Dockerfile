FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY netbox_ironic_controller ./netbox_ironic_controller
RUN pip install --no-cache-dir .
RUN useradd --system --uid 10001 rackd && chown -R rackd:rackd /app
USER rackd
EXPOSE 8080
CMD ["uvicorn", "netbox_ironic_controller.sync_api:app", "--host", "0.0.0.0", "--port", "8080"]
