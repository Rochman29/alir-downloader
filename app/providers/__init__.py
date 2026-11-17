from .youtube import YouTubeProvider


def default_providers():
    # Register a new provider here without changing the API or download queue.
    return [YouTubeProvider()]
