from starlette.exceptions import HTTPException


class BodyLimitMiddleware:
    """Enforce the limit on streamed requests too, before multipart can spool huge files."""
    def __init__(self, app, limit=21_000_000):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope['path'].startswith('/api/'):
            return await self.app(scope, receive, send)
        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            if message['type'] == 'http.request':
                size += len(message.get('body', b''))
                if size > self.limit:
                    raise HTTPException(413, 'Запрос больше 20 МБ')
            return message

        await self.app(scope, limited_receive, send)
