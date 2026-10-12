"""外部服务与站点的客户端，按服务分模块；这里统一导出，其余代码仍然写 ``from app.clients import ...``。"""

from ..parsers import site_proxy
from ._pool import _search_client, close_search_clients
from .ai import AIRecognitionClient
from .emby import EmbyClient
from .fanart import FANART_POSTER_URL, FanartClient, fanart_poster_rank
from .moviepilot import MoviePilotClient
from .sites import MTeamClient, NexusPHPClient, RSSClient, TorznabClient
from .tmdb import TMDBClient
from .transmission import TransmissionClient

__all__ = [
    "AIRecognitionClient", "EmbyClient", "FANART_POSTER_URL", "FanartClient", "MTeamClient", "MoviePilotClient",
    "NexusPHPClient", "RSSClient", "TMDBClient", "TorznabClient", "TransmissionClient",
    "_search_client", "close_search_clients", "fanart_poster_rank", "site_proxy",
]
