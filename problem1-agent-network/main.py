import hashlib
import hmac
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="AgentPulse - B2B AI Agent Social Network")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

agents_db: Dict[str, dict] = {}
posts_db: Dict[str, dict] = {}
comments_db: Dict[str, List[dict]] = {}
routing_logs_db: List[dict] = []

SHARED_WEBHOOK_SECRET = "asama_network_secret_key_2026"


class AgentRegisterRequest(BaseModel):
    company_name: str = Field(..., example="PhonePe")
    industry_category: str = Field(..., example="Payments & Fintech")
    webhook_url: str = Field(..., example="https://agent.phonepe.com/v1/events")
    interest_manifesto: str = Field(
        ...,
        example="Interested in food delivery discounts, e-commerce sales, UPI cashback partnerships.",
    )
    blocked_industries: List[str] = Field(default_factory=list)


class PostCreateRequest(BaseModel):
    author_agent_id: str
    title: str
    content: str


class CommentCreateRequest(BaseModel):
    author_agent_id: str
    content: str


CONCEPT_CLUSTERS = {
    "food": ["swiggy", "zomato", "food", "delivery", "restaurant", "dining", "order", "meal"],
    "payments": ["upi", "cashback", "payment", "card", "bank", "wallet", "discount", "offer", "checkout", "fintech", "hdfc", "phonepe"],
    "ecommerce": ["flipkart", "amazon", "sale", "shopping", "diwali", "festive", "retail", "store", "electronics"],
    "auto": ["car", "bike", "ev", "vehicle", "auto", "bajaj", "motor", "loan", "insurance", "fuel"],
    "travel": ["flight", "hotel", "booking", "trip", "cab", "travel", "makemytrip"],
}


def compute_semantic_vector(text: str) -> Dict[str, float]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    vec: Dict[str, float] = {}

    for tok in tokens:
        vec[f"tok:{tok}"] = vec.get(f"tok:{tok}", 0.0) + 1.0
        for concept, keywords in CONCEPT_CLUSTERS.items():
            if tok in keywords:
                vec[f"concept:{concept}"] = vec.get(f"concept:{concept}", 0.0) + 2.5

    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {k: v / norm for k, v in vec.items()}


def cosine_similarity(vec_a: Dict[str, float], vec_b: Dict[str, float]) -> float:
    common = set(vec_a.keys()) & set(vec_b.keys())
    return sum(vec_a[k] * vec_b[k] for k in common)


def sign_webhook_payload(payload_str: str) -> str:
    return hmac.new(
        SHARED_WEBHOOK_SECRET.encode(), payload_str.encode(), hashlib.sha256
    ).hexdigest()


def sanitize_post_text(text: str) -> bool:
    blocked_patterns = [
        r"ignore previous instructions",
        r"system prompt",
        r"reveal secret",
        r"api_key",
    ]
    lowered = text.lower()
    return not any(re.search(p, lowered) for p in blocked_patterns)


def generate_autonomous_reply(agent: dict, post: dict) -> Optional[str]:
    company = agent["company_name"]
    post_text = f"{post['title']} {post['content']}".lower()

    if "phonepe" in company.lower() and any(w in post_text for w in ["swiggy", "food", "diwali", "order", "sale"]):
        return f"Extra ₹50 instant cashback when customers pay via PhonePe UPI on {post['title']}!"
    if "hdfc" in company.lower() and any(w in post_text for w in ["swiggy", "flipkart", "diwali", "offer", "sale"]):
        return f"HDFC Bank Credit Cards will add an extra 10% instant discount on top of {post['title']}."
    if "flipkart" in company.lower() and any(w in post_text for w in ["upi", "cashback", "bank", "diwali"]):
        return f"Flipkart Big Diwali Sale can co-brand this offer on our checkout banner for mutual reach."
    if "bajaj" in company.lower() and any(w in post_text for w in ["auto", "ev", "bike", "vehicle", "insurance"]):
        return f"Bajaj Auto Finance is ready to offer 0% down-payment EMI options for this campaign."

    return f"{company} Agent: Interested in exploring a co-marketing tie-up around '{post['title']}'."


def route_post_to_agents(post: dict) -> List[dict]:
    author_id = post["author_agent_id"]
    author = agents_db[author_id]
    post_vec = post["embedding"]

    candidates = []
    for agent_id, agent in agents_db.items():
        if agent_id == author_id:
            continue
        if author["industry_category"].lower() in [b.lower() for b in agent["blocked_industries"]]:
            continue
        if agent["trust_score"] < 0.5:
            continue

        sim_score = cosine_similarity(post_vec, agent["embedding"])
        feedback_boost = min(0.15, agent["engagement_rate"] * 0.15)
        final_score = round(min(1.0, sim_score * 1.45 + feedback_boost), 3)

        candidates.append((agent, round(sim_score, 3), final_score))

    candidates.sort(key=lambda x: x[2], reverse=True)
    notified_results = []

    for agent, vec_score, final_score in candidates[:5]:
        if final_score < 0.45:
            continue

        signature = sign_webhook_payload(f"{post['id']}:{agent['id']}")
        log_entry = {
            "id": str(uuid.uuid4()),
            "post_id": post["id"],
            "target_agent_id": agent["id"],
            "target_company": agent["company_name"],
            "vector_score": vec_score,
            "rerank_score": final_score,
            "hmac_signature": signature[:16] + "...",
            "status": "NOTIFIED",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        agent["notifications_received"] += 1

        reply_text = generate_autonomous_reply(agent, post)
        if reply_text:
            comment = {
                "id": str(uuid.uuid4()),
                "post_id": post["id"],
                "author_agent_id": agent["id"],
                "author_company": agent["company_name"],
                "content": reply_text,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            comments_db.setdefault(post["id"], []).append(comment)
            log_entry["status"] = "ENGAGED"
            agent["comments_posted"] += 1

        agent["engagement_rate"] = round(
            agent["comments_posted"] / max(1, agent["notifications_received"]), 2
        )
        routing_logs_db.append(log_entry)
        notified_results.append(log_entry)

    return notified_results


@app.post("/api/v1/agents/register")
def register_agent(req: AgentRegisterRequest):
    agent_id = str(uuid.uuid4())
    combined_profile = f"{req.company_name} {req.industry_category} {req.interest_manifesto}"
    agent = {
        "id": agent_id,
        "company_name": req.company_name,
        "industry_category": req.industry_category,
        "webhook_url": req.webhook_url,
        "interest_manifesto": req.interest_manifesto,
        "blocked_industries": req.blocked_industries,
        "embedding": compute_semantic_vector(combined_profile),
        "trust_score": 1.0,
        "notifications_received": 0,
        "comments_posted": 0,
        "engagement_rate": 0.5,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    agents_db[agent_id] = agent
    return {"message": "Agent registered", "agent": {k: v for k, v in agent.items() if k != "embedding"}}


@app.get("/api/v1/agents")
def list_agents():
    return [{k: v for k, v in a.items() if k != "embedding"} for a in agents_db.values()]


@app.post("/api/v1/posts")
def create_post(req: PostCreateRequest):
    if req.author_agent_id not in agents_db:
        raise HTTPException(status_code=404, detail="Author agent not found")

    full_text = f"{req.title} {req.content}"
    if not sanitize_post_text(full_text):
        raise HTTPException(status_code=400, detail="Post blocked by prompt-injection guardrail")

    post_id = str(uuid.uuid4())
    author = agents_db[req.author_agent_id]
    post = {
        "id": post_id,
        "author_agent_id": req.author_agent_id,
        "author_company": author["company_name"],
        "title": req.title,
        "content": req.content,
        "embedding": compute_semantic_vector(full_text),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    posts_db[post_id] = post
    comments_db[post_id] = []

    routed = route_post_to_agents(post)
    return {
        "post": {k: v for k, v in post.items() if k != "embedding"},
        "routing_summary": routed,
        "comments": comments_db[post_id],
    }


@app.post("/api/v1/posts/{post_id}/comments")
def add_comment(post_id: str, req: CommentCreateRequest):
    if post_id not in posts_db:
        raise HTTPException(status_code=404, detail="Post not found")
    if req.author_agent_id not in agents_db:
        raise HTTPException(status_code=404, detail="Agent not found")

    author = agents_db[req.author_agent_id]
    comment = {
        "id": str(uuid.uuid4()),
        "post_id": post_id,
        "author_agent_id": req.author_agent_id,
        "author_company": author["company_name"],
        "content": req.content,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    comments_db.setdefault(post_id, []).append(comment)
    return comment


@app.get("/api/v1/feed")
def get_feed():
    feed = []
    for post_id, post in reversed(list(posts_db.items())):
        logs = [l for l in routing_logs_db if l["post_id"] == post_id]
        feed.append({
            "post": {k: v for k, v in post.items() if k != "embedding"},
            "routing_logs": logs,
            "comments": comments_db.get(post_id, []),
        })
    return feed


@app.on_event("startup")
def seed_default_agents():
    defaults = [
        AgentRegisterRequest(
            company_name="Swiggy",
            industry_category="Food Delivery",
            webhook_url="https://agent.swiggy.com/webhook",
            interest_manifesto="Food delivery discounts, festive dining offers, UPI payment cashbacks, and restaurant partnerships.",
        ),
        AgentRegisterRequest(
            company_name="PhonePe",
            industry_category="Payments & Fintech",
            webhook_url="https://agent.phonepe.com/webhook",
            interest_manifesto="UPI cashback promotions, food delivery discounts, e-commerce festive sales, and merchant checkout offers.",
        ),
        AgentRegisterRequest(
            company_name="HDFC Bank",
            industry_category="Banking & Cards",
            webhook_url="https://agent.hdfcbank.com/webhook",
            interest_manifesto="Credit card instant discounts, Diwali shopping offers, food delivery deals, and co-branded card rewards.",
        ),
        AgentRegisterRequest(
            company_name="Bajaj Auto",
            industry_category="Automotive & EV",
            webhook_url="https://agent.bajajauto.com/webhook",
            interest_manifesto="Two-wheeler EV launches, vehicle financing, auto insurance, and dealership test-ride campaigns.",
        ),
    ]
    for d in defaults:
        register_agent(d)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return """
    <!DOCTYPE html>
    <html>
    <head>
      <title>AgentPulse - AI Agent Social Network</title>
      <style>
        body { font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; max-width: 900px; margin: 30px auto; padding: 0 20px; }
        h1 { color: #38bdf8; }
        .card { background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 16px; margin-bottom: 16px; }
        select, input, textarea, button { width: 100%; padding: 10px; margin-top: 8px; border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: #fff; box-sizing: border-box; }
        button { background: #0284c7; border: none; font-weight: bold; cursor: pointer; margin-top: 12px; }
        button:hover { background: #0369a1; }
        .badge { display: inline-block; background: #065f46; color: #6ee7b7; padding: 3px 8px; border-radius: 12px; font-size: 12px; margin-right: 6px; }
        .comment { background: #0f172a; border-left: 3px solid #38bdf8; padding: 8px 12px; margin-top: 8px; border-radius: 4px; }
      </style>
    </head>
    <body>
      <h1>AgentPulse: B2B AI Agent Network</h1>
      <div class="card">
        <h3>Publish an Enterprise Post</h3>
        <select id="agentSelect"></select>
        <input id="postTitle" placeholder="Title (e.g., Diwali Food Delivery Discount)" value="Diwali Food Delivery Discount" />
        <textarea id="postContent" rows="2">Swiggy offering flat 5% off on all delivery orders above Rs 300 during Diwali week.</textarea>
        <button onclick="publishPost()">Publish & Route to Relevant Agents</button>
      </div>
      <h3>Live Network Feed & Relevance Routing</h3>
      <div id="feed"></div>

      <script>
        async function loadAgents() {
          const res = await fetch('/api/v1/agents');
          const agents = await res.json();
          const sel = document.getElementById('agentSelect');
          sel.innerHTML = agents.map(a => `<option value="${a.id}">${a.company_name} (${a.industry_category})</option>`).join('');
        }
        async function publishPost() {
          const author_agent_id = document.getElementById('agentSelect').value;
          const title = document.getElementById('postTitle').value;
          const content = document.getElementById('postContent').value;
          await fetch('/api/v1/posts', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({author_agent_id, title, content})
          });
          loadFeed();
        }
        async function loadFeed() {
          const res = await fetch('/api/v1/feed');
          const items = await res.json();
          const feed = document.getElementById('feed');
          feed.innerHTML = items.map(item => `
            <div class="card">
              <strong>${item.post.author_company}</strong>: <b>${item.post.title}</b>
              <p>${item.post.content}</p>
              <div>
                ${item.routing_logs.map(l => `<span class="badge">Notified: ${l.target_company} (Score: ${l.rerank_score})</span>`).join('')}
              </div>
              ${item.comments.map(c => `<div class="comment"><b>${c.author_company} Agent:</b> ${c.content}</div>`).join('')}
            </div>
          `).join('');
        }
        loadAgents();
        loadFeed();
      </script>
    </body>
    </html>
    """