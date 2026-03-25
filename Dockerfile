FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create data directories
RUN mkdir -p /app/data /app/geminiweb2api/static/images

# Set environment for data persistence
ENV DATA_DIR=/app/data

# Expose port
EXPOSE 8000

# Run
CMD ["python", "main.py", "--host", "0.0.0.0", "--port", "8000"]
