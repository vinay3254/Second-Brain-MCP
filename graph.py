import kuzu
import threading
from contextlib import contextmanager

class GraphDB:
    def __init__(self, db_path: str):
        self.db = kuzu.Database(db_path)
        self.lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def get_conn(self):
        """Context manager to obtain a connection."""
        conn = kuzu.Connection(self.db)
        try:
            yield conn
        finally:
            # Connections are cleaned up automatically
            pass

    def _init_schema(self):
        """Initializes tables if they do not exist."""
        with self.lock:
            with self.get_conn() as conn:
                try:
                    conn.execute("CREATE NODE TABLE Note (title STRING, body STRING, tags STRING[], last_modified STRING, PRIMARY KEY (title))")
                except Exception:
                    # Table already exists
                    pass

                try:
                    conn.execute("CREATE REL TABLE Links (FROM Note TO Note)")
                except Exception:
                    # Relationship already exists
                    pass

    def upsert_note(self, title: str, body: str, tags: list[str], last_modified: str, wikilinks: list[str]):
        """Upserts a note and updates its outgoing relationships."""
        with self.lock:
            with self.get_conn() as conn:
                # 1. Check if the note node already exists
                res = conn.execute("MATCH (n:Note) WHERE n.title = $title RETURN count(n)", {"title": title})
                exists = False
                if res.has_next():
                    exists = res.get_next()[0] > 0

                # 2. Insert or update the note node
                if exists:
                    conn.execute(
                        "MATCH (n:Note) WHERE n.title = $title SET n.body = $body, n.tags = $tags, n.last_modified = $last_modified",
                        {"title": title, "body": body, "tags": tags, "last_modified": last_modified}
                    )
                else:
                    conn.execute(
                        "CREATE (:Note {title: $title, body: $body, tags: $tags, last_modified: $last_modified})",
                        {"title": title, "body": body, "tags": tags, "last_modified": last_modified}
                    )

                # 3. Clean up existing outgoing Links relationships
                conn.execute("MATCH (a:Note)-[r:Links]->(b:Note) WHERE a.title = $title DELETE r", {"title": title})

                # 4. Handle wikilinks
                for target in wikilinks:
                    if target == title:
                        continue
                    
                    # Ensure target node exists (as a placeholder if it doesn't already)
                    target_res = conn.execute("MATCH (n:Note) WHERE n.title = $target RETURN count(n)", {"target": target})
                    target_exists = False
                    if target_res.has_next():
                        target_exists = target_res.get_next()[0] > 0

                    if not target_exists:
                        # Insert a placeholder node
                        conn.execute(
                            "CREATE (:Note {title: $target, body: '', tags: [], last_modified: ''})",
                            {"target": target}
                        )

                    # Create the Links relationship
                    try:
                        conn.execute(
                            "MATCH (a:Note), (b:Note) WHERE a.title = $source AND b.title = $target CREATE (a)-[:Links]->(b)",
                            {"source": title, "target": target}
                        )
                    except Exception:
                        # Skip if there's any duplicate link creation error
                        pass

    def delete_note(self, title: str):
        """Deletes a note and all its connected relationships."""
        with self.lock:
            with self.get_conn() as conn:
                conn.execute("MATCH (n:Note) WHERE n.title = $title DETACH DELETE n", {"title": title})

    def get_note(self, title: str) -> dict | None:
        """Retrieves a note's properties along with outgoing links and incoming backlinks."""
        with self.lock:
            with self.get_conn() as conn:
                # Get node properties
                res = conn.execute(
                    "MATCH (n:Note) WHERE n.title = $title RETURN n.body, n.tags, n.last_modified",
                    {"title": title}
                )
                if not res.has_next():
                    return None
                
                row = res.get_next()
                body, tags, last_modified = row[0], row[1], row[2]

                # Get outgoing links
                links_res = conn.execute(
                    "MATCH (a:Note)-[:Links]->(b:Note) WHERE a.title = $title RETURN b.title",
                    {"title": title}
                )
                links = []
                while links_res.has_next():
                    links.append(links_res.get_next()[0])

                # Get incoming backlinks
                backlinks_res = conn.execute(
                    "MATCH (b:Note)-[:Links]->(a:Note) WHERE a.title = $title RETURN b.title",
                    {"title": title}
                )
                backlinks = []
                while backlinks_res.has_next():
                    backlinks.append(backlinks_res.get_next()[0])

                return {
                    "title": title,
                    "body": body,
                    "tags": tags,
                    "last_modified": last_modified,
                    "links": links,
                    "backlinks": backlinks
                }

    def find_related(self, title: str) -> list[str]:
        """Finds 1-hop and 2-hop related note titles in the graph."""
        with self.lock:
            with self.get_conn() as conn:
                try:
                    res = conn.execute(
                        "MATCH (a:Note)-[:Links*1..2]-(b:Note) WHERE a.title = $title AND b.title <> $title RETURN DISTINCT b.title",
                        {"title": title}
                    )
                    related = []
                    while res.has_next():
                        related.append(res.get_next()[0])
                    return related
                except Exception:
                    # Fallback to manual 1-hop + 2-hop if variable-length path query fails
                    related = set()
                    # 1-hop outgoing
                    r1_out = conn.execute("MATCH (a:Note {title: $title})-[:Links]->(b:Note) RETURN b.title", {"title": title})
                    while r1_out.has_next():
                        related.add(r1_out.get_next()[0])
                    # 1-hop incoming
                    r1_in = conn.execute("MATCH (b:Note)-[:Links]->(a:Note {title: $title}) RETURN b.title", {"title": title})
                    while r1_in.has_next():
                        related.add(r1_in.get_next()[0])
                    
                    # 2-hop
                    hop1_list = list(related)
                    for h1 in hop1_list:
                        # Outgoing from h1
                        r2_out = conn.execute("MATCH (a:Note {title: $h1})-[:Links]->(b:Note) RETURN b.title", {"h1": h1})
                        while r2_out.has_next():
                            t = r2_out.get_next()[0]
                            if t != title:
                                related.add(t)
                        # Incoming to h1
                        r2_in = conn.execute("MATCH (b:Note)-[:Links]->(a:Note {title: $h1}) RETURN b.title", {"h1": h1})
                        while r2_in.has_next():
                            t = r2_in.get_next()[0]
                            if t != title:
                                related.add(t)
                    return sorted(list(related))

    def list_all_notes(self) -> list[dict]:
        """Returns all notes in the vault with title, tags, and last_modified."""
        with self.lock:
            with self.get_conn() as conn:
                res = conn.execute(
                    "MATCH (n:Note) RETURN n.title, n.tags, n.last_modified ORDER BY n.last_modified DESC"
                )
                notes = []
                while res.has_next():
                    row = res.get_next()
                    notes.append({
                        "title": row[0],
                        "tags": row[1],
                        "last_modified": row[2]
                    })
                return notes

    def get_daily_context(self, date_str: str) -> list[dict]:
        """Returns notes tagged with that date or modified on that day."""
        with self.lock:
            with self.get_conn() as conn:
                res = conn.execute(
                    "MATCH (n:Note) RETURN n.title, n.body, n.tags, n.last_modified"
                )
                results = []
                while res.has_next():
                    row = res.get_next()
                    n_title, n_body, n_tags, n_last_modified = row[0], row[1], row[2], row[3]
                    
                    is_match = False
                    # Check tags
                    if n_tags and any(date_str in tag for tag in n_tags):
                        is_match = True
                    # Check last_modified (format: YYYY-MM-DD)
                    elif n_last_modified and date_str in n_last_modified:
                        is_match = True
                        
                    if is_match:
                        results.append({
                            "title": n_title,
                            "body": n_body,
                            "tags": n_tags,
                            "last_modified": n_last_modified
                        })
                return results
