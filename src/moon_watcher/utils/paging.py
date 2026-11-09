def paginate(items: list, page: int, size: int = 8) -> tuple[list, int, int]:
    """Returns (items on the page, page number, total pages). Out of range pages get clamped."""
    pages = max(1, -(-len(items) // size))
    page = min(max(page, 0), pages - 1)
    return items[page * size:(page + 1) * size], page, pages
