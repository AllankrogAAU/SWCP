## Setup

```bash
# Build the runtime image (once, or whenever the Dockerfile changes)
docker build -t c-sandbox-runtime:latest .
```

## Usage

```bash
# Compile a submission (outside the sandbox, on trusted infra) NOTE - compile to a linux executable i.e. from WSL
gcc -Wall -Wextra submission.c -o submission

# Run it through the sandbox
python3 runner.py submission
```

Returned output is JSON in the following format:

```json
{
  "run_id": "8be88320-fe15-4d81-848a-be40a55306bf",
  "exit_code": 0,
  "timed_out": false,
  "duration_seconds": 0.293,
  "stdout": "Hello Sandbox!\n",
  "stderr": "",
  "stdout_truncated": false,
  "stderr_truncated": false
}
```
## Setup test suite
```bash 
cd tests
python3 -m venv venv
source venv/bin/activate
pip install pytest
```

## Running tests 

```bash
#This will catch all definitions with "test" in the start of the name
pytest -v
```
test