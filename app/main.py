import sqlite3
import math
import os
from typing import List, Optional
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
import httpx
from contextlib import asynccontextmanager
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, Counter, Histogram

# ACP Configuration
CP_API_KEY = os.getenv("CP_API_KEY")
CP_BASE_URL = os.getenv("CP_BASE_URL", "https://administrator.chmjk67.top")
AGENT_SLUG = os.getenv("AGENT_SLUG", "sunshine")
AGENT_ID = os.getenv("AGENT_ID")

DB_PATH = os.getenv("DB_PATH", "/app/gansu_rank_db.sqlite")

# Prometheus metrics
REQUEST_COUNT = Counter("app_requests_total", "Total requests", ["method", "endpoint"])
REQUEST_LATENCY = Histogram("app_request_duration_seconds", "Request latency")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: register agent with ACP"""
    if CP_API_KEY and CP_BASE_URL and AGENT_SLUG:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{CP_BASE_URL}/api/agents/register",
                    headers={"Authorization": f"Bearer {CP_API_KEY}"},
                    json={"slug": AGENT_SLUG, "endpoint": f"http://{AGENT_SLUG}_app:80"},
                    timeout=10.0,
                )
            print(f"[ACP] Agent {AGENT_SLUG} registered successfully")
        except Exception as e:
            print(f"[ACP] Registration failed: {e}")
    yield
    # Shutdown cleanup if needed


app = FastAPI(title="甘肃高考志愿助手", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


class RecommendRequest(BaseModel):
    rank: int = Field(..., ge=1, le=200000, description="考生位次")
    major: str = Field(default="计算机科学与技术", description="意向专业")
    top_n: int = Field(default=15, ge=5, le=30)


class SchoolRecommendation(BaseModel):
    category: str = Field(..., description="冲/稳/保")
    school_name: str = Field(...)
    major_group_name: str = Field(...)
    min_score: int = Field(...)
    min_rank: int = Field(...)
    avg_rank: int = Field(...)
    admission_probability: float = Field(...)
    recommend_reason: str = Field(...)
    risk_tip: str = Field(...)


class RecommendResponse(BaseModel):
    candidate_rank: int
    major: str
    total: int
    chong: List[SchoolRecommendation]
    wen: List[SchoolRecommendation]
    bao: List[SchoolRecommendation]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def calculate_probability(candidate_rank: int, rank_history: List[int]) -> float:
    if not rank_history or len(rank_history) < 1:
        return -1
    if len(rank_history) == 1:
        mean_rank = rank_history[0]
        std_dev = mean_rank * 0.1
    else:
        n = len(rank_history)
        mean_rank = sum(rank_history) / n
        variance = sum((r - mean_rank) ** 2 for r in rank_history) / (n - 1)
        std_dev = math.sqrt(variance) if variance > 0 else mean_rank * 0.1

    z_score = (mean_rank - candidate_rank) / std_dev
    a1, a2, a3, a4, a5, p = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429, 0.3275911
    sign = 1 if z_score >= 0 else -1
    z = abs(z_score) / math.sqrt(2)
    t = 1.0 / (1.0 + p * z)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-z * z)
    prob = 0.5 * (1.0 + sign * y) * 100
    return max(1.0, min(99.0, prob))


def get_recommend_reason(school: str, category: str, prob: float, candidate_rank: int, avg_rank: int) -> str:
    if category == "冲":
        return f"该校近年平均录取位次为{avg_rank}，您的位次{candidate_rank}略低于平均水平，但仍有{prob:.0f}%录取概率，可作为冲刺目标。"
    elif category == "稳":
        return f"该校近年平均录取位次为{avg_rank}，与您的位次{candidate_rank}接近，录取概率{prob:.0f}%，建议作为稳妥选择填报。"
    else:
        return f"该校近年平均录取位次为{avg_rank}，您的位次{candidate_rank}明显优于平均水平，录取概率{prob:.0f}%，可作为保底志愿。"


def get_risk_tip(category: str, prob: float) -> str:
    if category == "冲":
        return f"录取概率仅{prob:.0f}%，存在较大落榜风险，建议搭配更高概率志愿。"
    elif category == "稳":
        return f"录取概率{prob:.0f}%，风险适中，但仍需设置保底志愿。"
    else:
        return f"录取概率{prob:.0f}%，风险较低，但需注意专业调剂风险。"


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    REQUEST_COUNT.labels(method="GET", endpoint="/").inc()
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/recommend", response_model=RecommendResponse)
async def recommend_api(req: RecommendRequest):
    REQUEST_COUNT.labels(method="POST", endpoint="/api/recommend").inc()
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT school_name, major_group_name, min_score, min_rank, year
        FROM admission_data
        WHERE province = '甘肃' AND is_official = 1
        ORDER BY ABS(min_rank - ?)
        LIMIT ?
    """, (req.rank, req.top_n * 4))

    rows = cursor.fetchall()

    school_data = {}
    for row in rows:
        key = (row["school_name"], row["major_group_name"])
        if key not in school_data:
            school_data[key] = {
                "school": row["school_name"],
                "major": row["major_group_name"],
                "scores": [],
                "ranks": [],
            }
        school_data[key]["scores"].append(row["min_score"])
        school_data[key]["ranks"].append(row["min_rank"])

    results = []
    for (school, major), data in school_data.items():
        ranks = [r for r in data["ranks"] if r is not None]
        scores = [s for s in data["scores"] if s is not None]
        if not ranks or not scores:
            continue

        min_rank = min(ranks)
        avg_rank = int(sum(ranks) / len(ranks))
        min_score = min(scores)
        prob = calculate_probability(req.rank, ranks)

        if prob < 40:
            category = "冲"
        elif prob < 75:
            category = "稳"
        else:
            category = "保"

        results.append({
            "category": category,
            "school_name": school,
            "major_group_name": major,
            "min_score": min_score,
            "min_rank": min_rank,
            "avg_rank": avg_rank,
            "admission_probability": round(prob, 1),
            "recommend_reason": get_recommend_reason(school, category, prob, req.rank, avg_rank),
            "risk_tip": get_risk_tip(category, prob),
        })

    results.sort(key=lambda x: x["admission_probability"], reverse=True)

    chong = [r for r in results if r["category"] == "冲"][:5]
    wen = [r for r in results if r["category"] == "稳"][:5]
    bao = [r for r in results if r["category"] == "保"][:5]

    conn.close()

    return RecommendResponse(
        candidate_rank=req.rank,
        major=req.major,
        total=len(results),
        chong=chong,
        wen=wen,
        bao=bao,
    )


@app.post("/recommend", response_class=HTMLResponse)
async def recommend_form(request: Request, rank: int = Form(...), major: str = Form(default="计算机科学与技术")):
    REQUEST_COUNT.labels(method="POST", endpoint="/recommend").inc()
    req = RecommendRequest(rank=rank, major=major)
    result = await recommend_api(req)
    return templates.TemplateResponse("result.html", {"request": request, "result": result})


@app.get("/health")
async def health():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM admission_data WHERE is_official=1")
    count = cursor.fetchone()[0]
    conn.close()
    return {"status": "ok", "official_data_count": count}


@app.get("/metrics")
async def metrics():
    from fastapi.responses import Response
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/mcp/tools")
def list_tools():
    return {
        "tools": [
            {
                "name": "recommend_schools",
                "description": "根据考生位次推荐甘肃一本院校",
                "parameters": {
                    "rank": {"type": "integer", "description": "考生全省位次"},
                    "major": {"type": "string", "description": "意向专业"},
                },
            }
        ]
    }


@app.post("/mcp/tools/{tool_name}")
async def execute_tool(tool_name: str, request: dict):
    if tool_name == "recommend_schools":
        rank = request.get("rank", 0)
        major = request.get("major", "计算机科学与技术")
        req = RecommendRequest(rank=rank, major=major)
        result = await recommend_api(req)
        return {"result": result.model_dump()}
    return {"error": "Unknown tool"}
