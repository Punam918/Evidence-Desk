-- Documentation-level DDL checks on an isolated empty database after schema.sql.
-- Fixtures and temporary role roll back. This does not test application auth logic.
BEGIN;
INSERT INTO app.users(id,issuer,subject,email,display_name) VALUES
 ('00000000-0000-4000-8000-000000000001','fixture','one','one@example.test','One');
INSERT INTO app.embedding_profiles VALUES ('bge_small_en_v15','fixture-model','fixture-revision','fixture-tokenizer',384,true,'fixture-config',now());
INSERT INTO app.workspaces(id,name) VALUES
 ('00000000-0000-4000-8000-000000000011','A'),('00000000-0000-4000-8000-000000000012','B');
INSERT INTO app.knowledge_bases(workspace_id,id,name,created_by) VALUES
 ('00000000-0000-4000-8000-000000000011','00000000-0000-4000-8000-000000000021','A KB','00000000-0000-4000-8000-000000000001'),
 ('00000000-0000-4000-8000-000000000012','00000000-0000-4000-8000-000000000022','B KB','00000000-0000-4000-8000-000000000001');
CREATE ROLE ed_spec_runtime NOLOGIN NOSUPERUSER NOBYPASSRLS;
GRANT USAGE ON SCHEMA app TO ed_spec_runtime;
GRANT EXECUTE ON FUNCTION app.workspace_scope(),app.user_scope() TO ed_spec_runtime;
GRANT SELECT,INSERT ON app.knowledge_bases,app.documents TO ed_spec_runtime;
SET LOCAL ROLE ed_spec_runtime;
DO $$ BEGIN
 IF (SELECT count(*) FROM app.knowledge_bases) <> 0 THEN RAISE EXCEPTION 'Missing context leaked rows'; END IF;
END $$;
SET LOCAL app.workspace_id='00000000-0000-4000-8000-000000000011';
DO $$ BEGIN
 IF (SELECT count(*) FROM app.knowledge_bases) <> 1 THEN RAISE EXCEPTION 'Tenant filter failed'; END IF;
 BEGIN
  INSERT INTO app.knowledge_bases(workspace_id,id,name,created_by) VALUES
   ('00000000-0000-4000-8000-000000000012','00000000-0000-4000-8000-000000000023','Forbidden','00000000-0000-4000-8000-000000000001');
  RAISE EXCEPTION 'Cross-tenant insert unexpectedly succeeded';
 EXCEPTION WHEN insufficient_privilege THEN NULL;
 END;
 BEGIN
  INSERT INTO app.documents(workspace_id,id,kb_id,title,created_by) VALUES
   ('00000000-0000-4000-8000-000000000011','00000000-0000-4000-8000-000000000031','00000000-0000-4000-8000-000000000022','Wrong parent','00000000-0000-4000-8000-000000000001');
  RAISE EXCEPTION 'Cross-tenant parent unexpectedly accepted';
 EXCEPTION WHEN foreign_key_violation THEN NULL;
 END;
END $$;
RESET ROLE;
INSERT INTO app.conversations(workspace_id,id,user_id,title) VALUES
 ('00000000-0000-4000-8000-000000000011','00000000-0000-4000-8000-000000000041','00000000-0000-4000-8000-000000000001','Fixture');
INSERT INTO app.messages(workspace_id,id,conversation_id,user_id,sequence,role,state,text) VALUES
 ('00000000-0000-4000-8000-000000000011','00000000-0000-4000-8000-000000000051','00000000-0000-4000-8000-000000000041','00000000-0000-4000-8000-000000000001',1,'user','final','fixture');
INSERT INTO app.runs(workspace_id,id,conversation_id,user_id,user_message_id,status,intent,requested_model_key,config_sha256,prompt_version) VALUES
 ('00000000-0000-4000-8000-000000000011','00000000-0000-4000-8000-000000000061','00000000-0000-4000-8000-000000000041','00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000051','queued','ask','auto','fixture','fixture');
DO $$ BEGIN
 BEGIN
  INSERT INTO app.runs SELECT workspace_id,'00000000-0000-4000-8000-000000000062'::uuid,conversation_id,user_id,user_message_id,assistant_message_id,status,intent,model_key,requested_model_key,phase,config_sha256,prompt_version,execution_metadata,evidence,degraded,cancel_requested,next_event_seq,error_code,created_at,completed_at FROM app.runs;
  RAISE EXCEPTION 'Duplicate active run accepted';
 EXCEPTION WHEN unique_violation THEN NULL;
 END;
END $$;
INSERT INTO app.run_events(workspace_id,run_id,sequence,type,data) VALUES
 ('00000000-0000-4000-8000-000000000011','00000000-0000-4000-8000-000000000061',1,'run.failed','{}');
DO $$ BEGIN
 BEGIN
  INSERT INTO app.run_events(workspace_id,run_id,sequence,type,data) VALUES
   ('00000000-0000-4000-8000-000000000011','00000000-0000-4000-8000-000000000061',2,'run.cancelled','{}');
  RAISE EXCEPTION 'Duplicate terminal event accepted';
 EXCEPTION WHEN unique_violation THEN NULL;
 END;
END $$;
-- v2 manifest and thread ownership constraints.
INSERT INTO app.documents(workspace_id,id,kb_id,title,created_by) VALUES
 ('00000000-0000-4000-8000-000000000011','00000000-0000-4000-8000-000000000031','00000000-0000-4000-8000-000000000021','Source','00000000-0000-4000-8000-000000000001');
INSERT INTO app.document_revisions(workspace_id,id,document_id,kb_id,revision_no,status,object_key,mime_type,byte_size,content_sha256,parser_version,chunk_profile_id,profile_hash,embedding_profile_id)
 VALUES('00000000-0000-4000-8000-000000000011','00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000031','00000000-0000-4000-8000-000000000021',1,'verifying','fixture','text/plain',1,repeat('a',64),'fixture','fixture','fixture','bge_small_en_v15');
DO $$ BEGIN
 BEGIN
  INSERT INTO app.index_manifests(workspace_id,revision_id,profile_id,collection_name,state,expected_points,verified_points,manifest_sha256,artifact_key,verified_at)
  VALUES('00000000-0000-4000-8000-000000000011','00000000-0000-4000-8000-000000000071','bge_small_en_v15','fixture','verified',2,1,repeat('b',64),'fixture',now());
  RAISE EXCEPTION 'Incomplete verified manifest accepted';
 EXCEPTION WHEN check_violation THEN NULL;
 END;
END $$;
DO $$ BEGIN
 BEGIN
  INSERT INTO app.workflow_threads(workspace_id,id,user_id,run_id,graph_name,graph_version,status,expires_at)
  VALUES('00000000-0000-4000-8000-000000000011','00000000-0000-4000-8000-000000000082','00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000061','answer_v1','v1','running',now()+interval '24 hours');
  RAISE EXCEPTION 'Wrong thread owner accepted';
 EXCEPTION WHEN foreign_key_violation THEN NULL;
 END;
 INSERT INTO app.workflow_threads(workspace_id,id,user_id,run_id,graph_name,graph_version,status,expires_at)
 VALUES('00000000-0000-4000-8000-000000000011','00000000-0000-4000-8000-000000000081','00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000061','answer_v1','v1','running',now()+interval '24 hours');
 BEGIN
  INSERT INTO app.workflow_threads(workspace_id,id,user_id,run_id,graph_name,graph_version,status,expires_at)
  VALUES('00000000-0000-4000-8000-000000000011','00000000-0000-4000-8000-000000000083','00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000061','answer_v1','v1','running',now()+interval '24 hours');
  RAISE EXCEPTION 'Duplicate answer thread accepted';
 EXCEPTION WHEN unique_violation THEN NULL;
 END;
END $$;
ROLLBACK;
SELECT 'PASS: fail-closed RLS, tenant read/write fence, composite parent FK, one active run, one terminal event' AS result;
