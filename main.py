from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from typing import Dict, List, Set, Optional
from pydantic import BaseModel
from collections import defaultdict
from dataclasses import dataclass, field
import asyncio
import json
import uuid
import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_index():
    return FileResponse("index.html")

@app.get("/index.html")
async def serve_index_html():
    return FileResponse("index.html")

# ==================== TEAM TEMPLATES ====================
TEAM_TEMPLATES = [
    {"color": "#FF6B6B", "emoji": "🔴", "name": "Red"},
    {"color": "#4ECDC4", "emoji": "🔵", "name": "Blue"},
    {"color": "#FFE66D", "emoji": "🟡", "name": "Yellow"},
    {"color": "#A29BFE", "emoji": "🟣", "name": "Purple"},
    {"color": "#55EFC4", "emoji": "🟢", "name": "Green"},
    {"color": "#FD79A8", "emoji": "🩷", "name": "Pink"},
]

# ==================== PERSISTENCE ====================
ROOMS_FILE = "/tmp/rooms_state.json"
ROOM_TTL_SECONDS = 3600

def save_rooms_to_disk():
    try:
        data = {}
        now = datetime.now(timezone.utc).timestamp()
        for room_id, room in rooms.items():
            if now - room.created_at > ROOM_TTL_SECONDS:
                continue
            if room.state == "finished":
                continue
            data[room_id] = {
                "room_id": room.room_id,
                "quiz_id": room.quiz_id,
                "quiz_data": room.quiz_data,
                "state": "lobby",
                "created_at": room.created_at,
                "next_team_idx": room.next_team_idx,
                "teams": {
                    tid: {
                        "team_id": t.team_id,
                        "name": t.name,
                        "color": t.color,
                        "emoji": t.emoji,
                    }
                    for tid, t in room.teams.items()
                },
            }
        with open(ROOMS_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Failed to save rooms to disk: {e}")


def load_rooms_from_disk():
    try:
        if not os.path.exists(ROOMS_FILE):
            return
        with open(ROOMS_FILE, "r") as f:
            data = json.load(f)
        now = datetime.now(timezone.utc).timestamp()
        for room_id, rd in data.items():
            if now - rd.get("created_at", 0) > ROOM_TTL_SECONDS:
                continue
            room = Room(
                room_id=rd["room_id"],
                quiz_id=rd["quiz_id"],
                quiz_data=rd["quiz_data"],
                state=rd.get("state", "lobby"),
                created_at=rd.get("created_at", now),
                next_team_idx=rd.get("next_team_idx", 3),
            )
            for tid, td in rd.get("teams", {}).items():
                room.teams[tid] = Team(
                    team_id=td["team_id"],
                    name=td["name"],
                    color=td["color"],
                    emoji=td["emoji"],
                )
            rooms[room_id] = room
        logger.info(f"Loaded {len(rooms)} rooms from disk")
    except Exception as e:
        logger.error(f"Failed to load rooms from disk: {e}")


# ==================== DATACLASSES ====================
@dataclass
class Player:
    user_id: int
    username: str
    first_name: str
    team: str
    score: int = 0
    answered_questions: Set[int] = field(default_factory=set)

@dataclass
class Team:
    team_id: str
    name: str
    color: str
    emoji: str

@dataclass
class Room:
    room_id: str
    quiz_id: str
    quiz_data: dict
    players: Dict[int, Player] = field(default_factory=dict)
    teams: Dict[str, Team] = field(default_factory=dict)
    state: str = "lobby"
    current_question_idx: int = 0
    question_start_time: Optional[float] = None
    created_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    next_team_idx: int = 3
    # FIX #3: store full question log for export
    question_log: list = field(default_factory=list)

    def add_default_teams(self):
        for i in range(3):
            t = TEAM_TEMPLATES[i]
            tid = f"team_{i}"
            self.teams[tid] = Team(team_id=tid, name=t["name"], color=t["color"], emoji=t["emoji"])

    def add_team(self, custom_name: Optional[str] = None) -> Optional[Team]:
        if len(self.teams) >= len(TEAM_TEMPLATES):
            return None
        used_colors = {team.color for team in self.teams.values()}
        chosen = None
        for i in range(self.next_team_idx, self.next_team_idx + len(TEAM_TEMPLATES)):
            candidate = TEAM_TEMPLATES[i % len(TEAM_TEMPLATES)]
            if candidate["color"] not in used_colors:
                chosen = candidate
                break
        if not chosen:
            return None
        self.next_team_idx += 1
        tid = f"team_{len(self.teams)}"
        team_name = custom_name.strip() if custom_name and custom_name.strip() else chosen["name"]
        new_team = Team(team_id=tid, name=team_name, color=chosen["color"], emoji=chosen["emoji"])
        self.teams[tid] = new_team
        return new_team

    def teams_as_dict(self) -> dict:
        result = {}
        for tid, team in self.teams.items():
            result[tid] = {
                "team_id": tid,
                "name": team.name,
                "color": team.color,
                "emoji": team.emoji,
                "players": [
                    {
                        "user_id": p.user_id,
                        "username": p.username,
                        "first_name": p.first_name,
                        "score": p.score,
                    }
                    for p in self.players.values() if p.team == tid
                ],
            }
        return result

# ==================== GLOBAL STATE ====================
rooms: Dict[str, Room] = {}
connections: Dict[str, List[WebSocket]] = defaultdict(list)

# ==================== STARTUP ====================
@app.on_event("startup")
async def startup_event():
    load_rooms_from_disk()
    asyncio.create_task(cleanup_expired_rooms())

async def cleanup_expired_rooms():
    while True:
        await asyncio.sleep(300)
        now = datetime.now(timezone.utc).timestamp()
        expired = [
            rid for rid, room in rooms.items()
            if now - room.created_at > ROOM_TTL_SECONDS or room.state == "finished"
        ]
        for rid in expired:
            rooms.pop(rid, None)
            connections.pop(rid, None)
        if expired:
            logger.info(f"Cleaned up {len(expired)} expired rooms")
        save_rooms_to_disk()

# ==================== REST ENDPOINTS ====================

class CreateRoomRequest(BaseModel):
    quiz_id: str
    quiz_data: dict
    creator_id: int

@app.post("/api/create-room")
async def create_room(request: CreateRoomRequest):
    room_id = str(uuid.uuid4())[:8]
    room = Room(room_id=room_id, quiz_id=request.quiz_id, quiz_data=request.quiz_data)
    room.add_default_teams()
    rooms[room_id] = room
    save_rooms_to_disk()

    web_app_url = f"https://simplequizzerteamfightwebapp-production.up.railway.app?room={room_id}"
    return {"room_id": room_id, "web_app_url": web_app_url}


@app.get("/api/room/{room_id}")
async def get_room(room_id: str):
    if room_id not in rooms:
        return {"error": "Room not found"}
    room = rooms[room_id]
    return {
        "room_id": room.room_id,
        "quiz_title": room.quiz_data.get("title", "Quiz"),
        "state": room.state,
        "teams": room.teams_as_dict(),
        "total_questions": len(room.quiz_data.get("questions", [])),
    }

# FIX #3: Export results endpoint
@app.get("/api/room/{room_id}/results")
async def get_room_results(room_id: str):
    if room_id not in rooms:
        return JSONResponse({"error": "Room not found"}, status_code=404)
    room = rooms[room_id]
    
    quiz_data = room.quiz_data
    questions = quiz_data.get("questions", [])
    
    # Build per-question stats
    question_stats = []
    for qi, q in enumerate(questions):
        log_entry = room.question_log[qi] if qi < len(room.question_log) else {}
        answers = log_entry.get("answers", {})
        
        player_answers = []
        for uid_str, ans in answers.items():
            player_answers.append({
                "user_id": int(uid_str),
                "first_name": ans.get("first_name", "?"),
                "team": ans.get("team", ""),
                "is_correct": ans.get("is_correct", False),
                "points": ans.get("points", 0),
                "answer_idx": ans.get("answer_idx", -1),
            })
        
        question_stats.append({
            "question_idx": qi,
            "question_text": q.get("question", ""),
            "options": q.get("options", []),
            "correct_answer": q.get("correct_answer"),
            "player_answers": player_answers,
        })
    
    # Leaderboard
    sorted_players = sorted(room.players.values(), key=lambda p: p.score, reverse=True)
    leaderboard = [
        {
            "user_id": p.user_id,
            "first_name": p.first_name,
            "team": p.team,
            "score": p.score,
        }
        for p in sorted_players
    ]
    
    team_scores = {tid: 0 for tid in room.teams}
    for player in room.players.values():
        if player.team in team_scores:
            team_scores[player.team] += player.score
    
    winner_team = max(team_scores, key=team_scores.get) if team_scores else None
    
    return {
        "room_id": room_id,
        "quiz_title": quiz_data.get("title", "Quiz"),
        "total_questions": len(questions),
        "teams": room.teams_as_dict(),
        "team_scores": team_scores,
        "winner_team": winner_team,
        "leaderboard": leaderboard,
        "question_stats": question_stats,
    }

# ==================== WEBSOCKET ====================

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()

    if room_id not in rooms:
        await websocket.send_json({"type": "error", "message": "Room not found"})
        await websocket.close()
        return

    connections[room_id].append(websocket)
    user_id = None

    try:
        room = rooms[room_id]
        await websocket.send_json({
            "type": "room_state",
            "teams": room.teams_as_dict(),
            "state": room.state,
        })

        async for raw in websocket.iter_text():
            data = json.loads(raw)
            msg_type = data.get("type")
            room = rooms[room_id]

            # ----- ADD TEAM -----
            if msg_type == "add_team":
                custom_name = data.get("custom_name")
                new_team = room.add_team(custom_name=custom_name)
                if new_team:
                    save_rooms_to_disk()
                    await broadcast(room_id, {
                        "type": "teams_updated",
                        "teams": room.teams_as_dict(),
                    })
                else:
                    await websocket.send_json({"type": "error", "message": "Maximum 6 teams reached"})

            # ----- JOIN -----
            elif msg_type == "join":
                user_id = data["user_id"]
                username = data.get("username", "")
                first_name = data.get("first_name", "Player")
                team = data.get("team", list(room.teams.keys())[0])

                if team not in room.teams:
                    team = list(room.teams.keys())[0]

                if user_id not in room.players:
                    room.players[user_id] = Player(
                        user_id=user_id,
                        username=username,
                        first_name=first_name,
                        team=team,
                    )
                else:
                    room.players[user_id].team = team

                # FIX #6: Broadcast full teams dict so all clients see new player
                await broadcast(room_id, {
                    "type": "player_joined",
                    "teams": room.teams_as_dict(),
                    "player": {
                        "user_id": user_id,
                        "first_name": first_name,
                        "username": username,
                        "team": team,
                    }
                })

            # ----- START GAME -----
            elif msg_type == "start_game":
                teams_with_players = sum(
                    1 for tid in room.teams
                    if any(p.team == tid for p in room.players.values())
                )
                if len(room.players) < 2 or teams_with_players < 2:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Need at least 2 players on different teams",
                    })
                    continue

                if room.state != "lobby":
                    continue

                room.state = "countdown"

                for i in range(3, 0, -1):
                    await broadcast(room_id, {"type": "countdown", "seconds": i})
                    await asyncio.sleep(1)

                room.state = "playing"
                asyncio.create_task(run_quiz(room_id))

            # ----- ANSWER -----
            elif msg_type == "answer":
                player = room.players.get(user_id)
                if not player or room.state != "playing":
                    continue

                question_idx = data["question_idx"]
                answer_idx = data["answer"]

                if question_idx in player.answered_questions:
                    continue
                player.answered_questions.add(question_idx)

                question = room.quiz_data["questions"][question_idx]
                correct_answer = question.get("correct_answer")

                if isinstance(correct_answer, int):
                    is_correct = answer_idx == correct_answer
                else:
                    options = question["options"]
                    is_correct = (
                        answer_idx < len(options) and options[answer_idx] == correct_answer
                    )

                points = 0
                if is_correct:
                    time_elapsed = datetime.now(timezone.utc).timestamp() - (room.question_start_time or 0)
                    quiz_time = room.quiz_data.get("quiz_time", 30)
                    time_ratio = max(0.0, 1.0 - (time_elapsed / max(quiz_time, 1)))
                    points = int(500 + (500 * time_ratio))
                    player.score += points

                # FIX #3/#5: Log answer in room for export and stats
                while len(room.question_log) <= question_idx:
                    room.question_log.append({"answers": {}})
                room.question_log[question_idx]["answers"][str(user_id)] = {
                    "first_name": player.first_name,
                    "team": player.team,
                    "is_correct": is_correct,
                    "points": points,
                    "answer_idx": answer_idx,
                }

                await broadcast(room_id, {
                    "type": "answer_submitted",
                    "user_id": user_id,
                    "first_name": player.first_name,
                    "team": player.team,
                    "is_correct": is_correct,
                    "points": points,
                    "new_score": player.score,
                    # FIX #4: Do NOT send correct_answer here — only reveal after question ends
                })

    except WebSocketDisconnect:
        pass
    finally:
        if websocket in connections[room_id]:
            connections[room_id].remove(websocket)
        if user_id and room_id in rooms:
            room = rooms[room_id]
            if user_id in room.players:
                del room.players[user_id]
                await broadcast(room_id, {
                    "type": "player_left",
                    "user_id": user_id,
                    "teams": room.teams_as_dict(),
                })

# ==================== HELPERS ====================

async def broadcast(room_id: str, message: dict):
    for ws in connections[room_id]:
        try:
            await ws.send_json(message)
        except Exception:
            pass

async def run_quiz(room_id: str):
    room = rooms[room_id]
    questions = room.quiz_data.get("questions", [])
    quiz_time = room.quiz_data.get("quiz_time", 30)

    for idx, question in enumerate(questions):
        if room_id not in rooms:
            return
        room = rooms[room_id]
        if room.state != "playing":
            return

        room.current_question_idx = idx
        room.question_start_time = datetime.now(timezone.utc).timestamp()

        # Ensure log slot exists
        while len(room.question_log) <= idx:
            room.question_log.append({"answers": {}})

        # FIX #4: Send question WITHOUT correct_answer
        await broadcast(room_id, {
            "type": "question",
            "question_idx": idx,
            "question_text": question["question"],
            "options": question["options"],
            "time_limit": quiz_time,
            "total_questions": len(questions),
            # correct_answer intentionally omitted here
        })

        await asyncio.sleep(quiz_time)

        # FIX #4: Reveal correct answer AFTER time is up
        correct_answer = question.get("correct_answer")
        options = question.get("options", [])
        correct_idx = correct_answer if isinstance(correct_answer, int) else (
            options.index(correct_answer) if correct_answer in options else -1
        )
        await broadcast(room_id, {
            "type": "question_ended",
            "question_idx": idx,
            "correct_option_idx": correct_idx,
            # FIX #5: Send per-question stats to all clients
            "question_answers": room.question_log[idx].get("answers", {}),
        })

        if idx < len(questions) - 1:
            await asyncio.sleep(2.5)

    await finish_quiz(room_id)


async def finish_quiz(room_id: str):
    if room_id not in rooms:
        return
    room = rooms[room_id]
    room.state = "finished"

    team_scores = {tid: 0 for tid in room.teams}
    for player in room.players.values():
        if player.team in team_scores:
            team_scores[player.team] += player.score

    winner_team = max(team_scores, key=team_scores.get) if team_scores else None
    sorted_players = sorted(room.players.values(), key=lambda p: p.score, reverse=True)

    # FIX #5: Include per-question log in finish payload
    question_log_payload = []
    for qi, entry in enumerate(room.question_log):
        q = room.quiz_data["questions"][qi] if qi < len(room.quiz_data["questions"]) else {}
        options = q.get("options", [])
        correct_answer = q.get("correct_answer")
        correct_idx = correct_answer if isinstance(correct_answer, int) else (
            options.index(correct_answer) if correct_answer in options else -1
        )
        question_log_payload.append({
            "question_text": q.get("question", ""),
            "options": options,
            "correct_idx": correct_idx,
            "answers": entry.get("answers", {}),
        })

    await broadcast(room_id, {
        "type": "quiz_finished",
        "team_scores": team_scores,
        "teams": room.teams_as_dict(),
        "leaderboard": [
            {
                "user_id": p.user_id,
                "first_name": p.first_name,
                "team": p.team,
                "score": p.score,
            }
            for p in sorted_players
        ],
        "winner_team": winner_team,
        "question_log": question_log_payload,
    })

    # FIX #2: Keep room alive for results access (don't delete immediately)
    # Clean up after 30 min instead of 5
    await asyncio.sleep(1800)
    rooms.pop(room_id, None)
    connections.pop(room_id, None)
    save_rooms_to_disk()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, workers=1)