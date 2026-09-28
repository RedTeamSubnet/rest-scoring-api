"""Small, dependency-free helpers shared by API modules."""


def get_docker_hub_id(plain_commit: object) -> str | None:
    """Extract the revealed Docker image identifier from a plain commit."""
    if not isinstance(plain_commit, str) or "---" not in plain_commit:
        return None
    _, docker_hub_id = plain_commit.split("---", 1)
    return docker_hub_id or None
