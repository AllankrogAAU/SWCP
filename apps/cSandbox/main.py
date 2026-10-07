from fastapi import FastAPI

from api.submissions import router as submissions_router

app = FastAPI(title="c-sandbox")
app.include_router(submissions_router)
