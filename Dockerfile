FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system updates and essential tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip to prevent installation glitches
RUN pip install --no-cache-dir --upgrade pip

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . .

# Expose the dynamic port Render uses
EXPOSE 10000

# Run the bot with unbuffered output so logs show up instantly
CMD ["python", "-u", "app.py"]
