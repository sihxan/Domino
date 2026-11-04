CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT entity_key IF NOT EXISTS FOR (n:Entity) REQUIRE (n.name, n.type, n.region) IS UNIQUE;
CREATE CONSTRAINT event_id IF NOT EXISTS FOR (n:Event) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT source_url IF NOT EXISTS FOR (n:Source) REQUIRE n.url IS UNIQUE;
CREATE CONSTRAINT evidence_id IF NOT EXISTS FOR (n:Evidence) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT story_id IF NOT EXISTS FOR (n:Story) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT storyline_id IF NOT EXISTS FOR (n:Storyline) REQUIRE n.id IS UNIQUE;

CREATE INDEX evidence_edge IF NOT EXISTS FOR (n:Evidence) ON (n.edge_id);
CREATE INDEX event_time IF NOT EXISTS FOR (n:Event) ON (n.occurred_at);
CREATE INDEX event_sched IF NOT EXISTS FOR (n:Event) ON (n.scheduled_at);
CREATE INDEX causes_temporal IF NOT EXISTS FOR ()-[r:CAUSES]-() ON (r.created_at, r.expired_at, r.valid_at, r.invalid_at);
CREATE INDEX causes_status IF NOT EXISTS FOR ()-[r:CAUSES]-() ON (r.status, r.confidence);

CREATE FULLTEXT INDEX entity_text IF NOT EXISTS FOR (n:Entity) ON EACH [n.name, n.aliases];
CREATE VECTOR INDEX entity_embedding IF NOT EXISTS FOR (n:Entity) ON (n.embedding)
  OPTIONS { indexConfig: { `vector.dimensions`: 1024, `vector.similarity_function`: 'cosine' } };
CREATE VECTOR INDEX evidence_embedding IF NOT EXISTS FOR (n:Evidence) ON (n.embedding)
  OPTIONS { indexConfig: { `vector.dimensions`: 1024, `vector.similarity_function`: 'cosine' } };
CREATE VECTOR INDEX storyline_embedding IF NOT EXISTS FOR (n:Storyline) ON (n.embedding)
  OPTIONS { indexConfig: { `vector.dimensions`: 1024, `vector.similarity_function`: 'cosine' } };
