"""Единая точка выбора LLM-провайдера. Переключить разом — поменять LLM_PROVIDER
в .env, не трогая код парсера.

Зачем вынесено: 2026-08-17 Groq снял модель `llama-3.1-8b-instant`, захардкоженную
в date_parser.py. Каждый ввод времени падал в 404, бот отвечал «Не понял запрос» и
сбрасывал диалог — воронка записи стояла три дня, узнали от клиента. Теперь имя
модели и провайдер живут в конфиге, а не в коде.

Основной провайдер — ProxyAPI (OpenAI-совместимый, gpt-4o-mini), тот же ключ и та же
схема, что в review-monitor. Задача тут простая (вытащить дату/время в JSON), полный
gpt-4o не нужен. Groq остаётся запасным — переключается через LLM_PROVIDER=groq.
"""
from __future__ import annotations

from config import LLM_MODEL, LLM_PROVIDER, PROXYAPI_BASE_URL, PROXYAPI_KEY, GROQ_API_KEY

_DEFAULT_MODELS = {"proxyapi": "gpt-4o-mini", "groq": "llama-3.3-70b-versatile"}

PROVIDER = LLM_PROVIDER if LLM_PROVIDER in _DEFAULT_MODELS else "proxyapi"
MODEL = LLM_MODEL or _DEFAULT_MODELS[PROVIDER]


def get_client():
    """OpenAI-совместимый клиент. Импорты ленивые — чтобы отсутствие пакета
    неиспользуемого провайдера не роняло бота на старте."""
    if PROVIDER == "groq":
        from groq import Groq
        return Groq(api_key=GROQ_API_KEY)
    from openai import OpenAI
    return OpenAI(api_key=PROXYAPI_KEY, base_url=PROXYAPI_BASE_URL)
