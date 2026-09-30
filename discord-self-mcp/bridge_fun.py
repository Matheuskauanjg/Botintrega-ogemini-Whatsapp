import ast
import asyncio
import operator
import os
import random
import time
from typing import Any

import httpx

import bridge_join_announce as stack

app = stack.app
voice = stack.voice
auto = stack.auto
base = auto.base
client = auto.client

STARTED_AT = time.monotonic()
GROQ_COMMAND_MODEL = os.getenv("GROQ_COMMAND_MODEL", "openai/gpt-oss-120b").strip()

EIGHT_BALL = [
    "com certeza",
    "provavelmente sim",
    "eu apostaria que sim",
    "melhor não contar com isso",
    "tá com cara de não",
    "pergunta de novo depois kkkkk",
    "os servidores disseram talvez",
    "100% confiável: não faço ideia",
]

FUN_COMMANDS = {
    "/coin | /moeda": "cara ou coroa e fala o resultado",
    "/dice [faces] | /dado [faces]": "rola um dado; padrão 6, máximo 1000",
    "/choose a | b | c": "escolhe uma opção aleatoriamente",
    "/8ball <pergunta>": "bola 8 com resposta aleatória",
    "/mock <texto>": "repete o texto em estilo zueira",
    "/randomsound": "toca um efeito aleatório do soundboard",
    "/countdown <1-10>": "faz uma contagem regressiva curta",
}

UTILITY_COMMANDS = {
    "/ping": "mostra a latência do Discord",
    "/uptime": "tempo desde que o bridge iniciou",
    "/whoami": "mostra a conta conectada",
    "/channel": "mostra servidor e call atual",
    "/members": "lista participantes visíveis na call atual",
    "/random <min> <max>": "sorteia um inteiro no intervalo",
    "/calc <expressão>": "calcula operações matemáticas básicas com segurança",
    "/ask <pergunta>": "faz uma pergunta curta ao Groq e fala a resposta",
}

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, total = divmod(total, 86400)
    hours, total = divmod(total, 3600)
    minutes, secs = divmod(total, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _safe_calc(expression: str) -> float | int:
    if len(expression) > 160:
        raise ValueError("expressão muito longa")
    tree = ast.parse(expression, mode="eval")

    def walk(node: ast.AST, depth: int = 0) -> float | int:
        if depth > 12:
            raise ValueError("expressão complexa demais")
        if isinstance(node, ast.Expression):
            return walk(node.body, depth + 1)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            if abs(float(node.value)) > 1e12:
                raise ValueError("número muito grande")
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
            left = walk(node.left, depth + 1)
            right = walk(node.right, depth + 1)
            if isinstance(node.op, ast.Pow) and abs(float(right)) > 12:
                raise ValueError("expoente muito grande")
            result = _ALLOWED_BINOPS[type(node.op)](left, right)
            if abs(float(result)) > 1e15:
                raise ValueError("resultado muito grande")
            return result
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
            return _ALLOWED_UNARY[type(node.op)](walk(node.operand, depth + 1))
        raise ValueError("use apenas números, parênteses e + - * / // % **")

    return walk(tree)


def _mock_text(text: str) -> str:
    out = []
    upper = False
    for char in text:
        if char.isalpha():
            out.append(char.upper() if upper else char.lower())
            upper = not upper
        else:
            out.append(char)
    return "".join(out)


async def _ask_groq(prompt: str) -> str:
    if not base.GROQ_API_KEY:
        return "Groq não está configurado agora."
    payload = {
        "model": GROQ_COMMAND_MODEL,
        "temperature": 0.7,
        "max_completion_tokens": 180,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Responda em português do Brasil para ser falado em uma call do Discord. "
                    "Seja direto, natural e curto: normalmente uma ou duas frases. "
                    "Não revele credenciais, tokens ou segredos e não faça ameaças reais."
                ),
            },
            {"role": "user", "content": prompt[:1200]},
        ],
    }
    async with httpx.AsyncClient(timeout=25) as http:
        response = await http.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {base.GROQ_API_KEY}", "Content-Type": "application/json"},
            json=payload,
        )
    if response.is_error:
        detail = response.text.replace("\n", " ")[:300]
        raise RuntimeError(f"Groq HTTP {response.status_code}: {detail}")
    text = str(response.json()["choices"][0]["message"]["content"]).strip()
    return " ".join(text.split())[:700]


async def _speak_and_return(target: str, text: str, command: str, **extra: Any) -> dict[str, Any]:
    spoken = await voice.speak_robot(target, text)
    return {
        "ok": True,
        "mode": "fun-command",
        "command": command,
        "text": text,
        "voice": spoken.get("voice", {}),
        **extra,
    }


_original_handle_voice_send = voice.handle_voice_send


async def handle_voice_send_with_fun(body: Any) -> dict[str, Any]:
    target = body.to[len(voice.VOICE_PREFIX):].strip() if body.to.casefold().startswith(voice.VOICE_PREFIX) else ""
    message = body.message.strip()
    folded = message.casefold()

    if folded in {"/help", "!help", "/ajuda", "!ajuda"}:
        result = await _original_handle_voice_send(body)
        commands = result.setdefault("commands", {})
        commands.update(FUN_COMMANDS)
        commands.update(UTILITY_COMMANDS)
        return result

    if folded in {"/fun", "!fun", "/zoeira", "!zoeira"}:
        return {"ok": True, "mode": "voice-command", "category": "fun", "commands": FUN_COMMANDS}

    if folded in {"/utils", "!utils", "/uteis", "!uteis", "/úteis", "!úteis"}:
        return {"ok": True, "mode": "voice-command", "category": "utilities", "commands": UTILITY_COMMANDS}

    if folded in {"/coin", "!coin", "/moeda", "!moeda"}:
        result = random.choice(["cara", "coroa"])
        return await _speak_and_return(target, f"Deu {result}.", "coin", result=result)

    if folded == "/dice" or folded == "!dice" or folded == "/dado" or folded == "!dado" or folded.startswith(("/dice ", "!dice ", "/dado ", "!dado ")):
        parts = message.split(maxsplit=1)
        sides = 6
        if len(parts) == 2:
            try:
                sides = int(parts[1])
            except ValueError as exc:
                raise voice.HTTPException(status_code=400, detail="Use /dice [2-1000]") from exc
        if sides < 2 or sides > 1000:
            raise voice.HTTPException(status_code=400, detail="O dado deve ter entre 2 e 1000 faces")
        result = random.randint(1, sides)
        return await _speak_and_return(target, f"Dado de {sides} faces: {result}.", "dice", sides=sides, result=result)

    if folded.startswith(("/choose ", "!choose ", "/escolhe ", "!escolhe ")):
        raw = message.split(maxsplit=1)[1]
        options = [item.strip() for item in raw.split("|") if item.strip()]
        if len(options) < 2 or len(options) > 20:
            raise voice.HTTPException(status_code=400, detail="Use /choose opção 1 | opção 2 | opção 3")
        chosen = random.choice(options)
        return await _speak_and_return(target, f"Eu escolho: {chosen}.", "choose", options=options, chosen=chosen)

    if folded.startswith(("/8ball ", "!8ball ", "/bola8 ", "!bola8 ")):
        question = message.split(maxsplit=1)[1].strip()
        if not question:
            raise voice.HTTPException(status_code=400, detail="Use /8ball <pergunta>")
        answer = random.choice(EIGHT_BALL)
        return await _speak_and_return(target, answer, "8ball", question=question, answer=answer)

    if folded.startswith(("/mock ", "!mock ", "/zoa ", "!zoa ")):
        text = message.split(maxsplit=1)[1].strip()[:400]
        if not text:
            raise voice.HTTPException(status_code=400, detail="Use /mock <texto>")
        mocked = _mock_text(text)
        return await _speak_and_return(target, mocked, "mock", result=mocked)

    if folded in {"/randomsound", "!randomsound", "/somaleatorio", "!somaleatorio", "/somaleatório", "!somaleatório"}:
        sound = random.choice(list(voice.SOUNDBOARD_NAMES))
        result = await voice.play_soundboard(target, sound)
        result["command"] = "randomsound"
        return result

    if folded.startswith(("/countdown ", "!countdown ", "/contagem ", "!contagem ")):
        raw = message.split(maxsplit=1)[1].strip()
        try:
            count = int(raw)
        except ValueError as exc:
            raise voice.HTTPException(status_code=400, detail="Use /countdown <1-10>") from exc
        if count < 1 or count > 10:
            raise voice.HTTPException(status_code=400, detail="A contagem deve ser de 1 a 10")
        text = " ... ".join(str(i) for i in range(count, 0, -1)) + " ... já!"
        return await _speak_and_return(target, text, "countdown", count=count)

    if folded in {"/ping", "!ping", "/latency", "!latency", "/latencia", "!latencia", "/latência", "!latência"}:
        latency_ms = round(float(getattr(client, "latency", 0.0) or 0.0) * 1000)
        text = f"Ping do Discord: {latency_ms} milissegundos."
        return await _speak_and_return(target, text, "ping", latencyMs=latency_ms)

    if folded in {"/uptime", "!uptime"}:
        uptime = _format_duration(time.monotonic() - STARTED_AT)
        return await _speak_and_return(target, f"Estou online há {uptime}.", "uptime", uptime=uptime)

    if folded in {"/whoami", "!whoami", "/quem", "!quem"}:
        user = client.user
        name = str(getattr(user, "display_name", None) or getattr(user, "name", "desconhecido"))
        user_id = str(getattr(user, "id", ""))
        return await _speak_and_return(target, f"Conta conectada: {name}.", "whoami", user={"id": user_id, "name": name})

    if folded in {"/channel", "!channel", "/call", "!call"}:
        state = voice.voice_state_payload()
        if not state.get("connected"):
            return {"ok": True, "mode": "voice-command", "command": "channel", "voice": state, "text": "Fora da call"}
        text = f"Estou na call {state.get('channelName')} do servidor {state.get('guildName')}."
        return await _speak_and_return(target, text, "channel", state=state)

    if folded in {"/members", "!members", "/membros", "!membros"}:
        vc = voice.active_voice_client
        channel = getattr(vc, "channel", None) if vc else None
        members = list(getattr(channel, "members", None) or [])
        names = [str(getattr(member, "display_name", None) or getattr(member, "name", member.id)) for member in members]
        if not names:
            return {"ok": True, "mode": "voice-command", "command": "members", "members": [], "text": "Nenhum participante visível na call."}
        spoken_names = ", ".join(names[:12])
        suffix = f" e mais {len(names) - 12}" if len(names) > 12 else ""
        text = f"Na call: {spoken_names}{suffix}."
        return await _speak_and_return(target, text, "members", members=names)

    if folded.startswith(("/random ", "!random ", "/numero ", "!numero ", "/número ", "!número ")):
        parts = message.split()
        if len(parts) != 3:
            raise voice.HTTPException(status_code=400, detail="Use /random <min> <max>")
        try:
            low, high = int(parts[1]), int(parts[2])
        except ValueError as exc:
            raise voice.HTTPException(status_code=400, detail="Os limites precisam ser inteiros") from exc
        if low > high:
            low, high = high, low
        if high - low > 10_000_000:
            raise voice.HTTPException(status_code=400, detail="Intervalo grande demais")
        result = random.randint(low, high)
        return await _speak_and_return(target, f"Número sorteado: {result}.", "random", minimum=low, maximum=high, result=result)

    if folded.startswith(("/calc ", "!calc ", "/calcula ", "!calcula ")):
        expression = message.split(maxsplit=1)[1].strip()
        try:
            result = _safe_calc(expression)
        except (SyntaxError, ValueError, ZeroDivisionError, OverflowError) as exc:
            raise voice.HTTPException(status_code=400, detail=f"Não consegui calcular: {exc}") from exc
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        result_text = f"{result:.10g}" if isinstance(result, float) else str(result)
        return await _speak_and_return(target, f"Resultado: {result_text}.", "calc", expression=expression, result=result)

    if folded.startswith(("/ask ", "!ask ", "/pergunta ", "!pergunta ")):
        prompt = message.split(maxsplit=1)[1].strip()
        if not prompt:
            raise voice.HTTPException(status_code=400, detail="Use /ask <pergunta>")
        try:
            answer = await _ask_groq(prompt)
        except Exception as exc:
            raise voice.HTTPException(status_code=502, detail=f"Falha no Groq: {type(exc).__name__}: {exc}") from exc
        return await _speak_and_return(target, answer, "ask", question=prompt, answer=answer)

    return await _original_handle_voice_send(body)


voice.handle_voice_send = handle_voice_send_with_fun
