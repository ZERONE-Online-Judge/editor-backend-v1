"""Upgrade existing filing in place; never fire SET NULL during a rebuild."""
import json
import os
from pathlib import Path
import subprocess
import sys
from sqlalchemy import create_engine, text
from app.schema import empty_document


def test_existing_filing_survives_upgrade_and_rollback(tmp_path):
    env={**os.environ,'EDITOR_DATABASE_URL':'sqlite:///'+str(tmp_path/'migration.sqlite')}
    root=Path(__file__).resolve().parents[1]
    def migrate(*args):
        result=subprocess.run([sys.executable,'-m','alembic',*args],cwd=root,env=env,capture_output=True,text=True)
        assert result.returncode==0,result.stdout+result.stderr
    migrate('upgrade','e72b134fa890')
    engine=create_engine(env['EDITOR_DATABASE_URL'])
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(id,email,handle,display_name,created_at) VALUES (:id,'old@example.com','old','Old',1)"),{'id':'a'*32})
        c.execute(text("INSERT INTO folders(id,user_id,parent_id,parent_key,name,name_key) VALUES (:id,:owner,NULL,'','Personal','personal')"),{'id':'b'*32,'owner':'a'*32})
        c.execute(text("INSERT INTO folders(id,user_id,parent_id,parent_key,name,name_key) VALUES (:id,:owner,:parent,:parent,'Child','child')"),{'id':'c'*32,'owner':'a'*32,'parent':'b'*32})
        c.execute(text("INSERT INTO problems(id,owner_id,slug,revision,document,deleted,note,created_at,updated_at) VALUES (:id,:owner,'old-task',0,:doc,0,'',1,1)"),{'id':'d'*32,'owner':'a'*32,'doc':json.dumps(empty_document('Old'))})
        c.execute(text("INSERT INTO problem_placements(user_id,problem_id,folder_id) VALUES (:owner,:problem,:folder)"),{'owner':'a'*32,'problem':'d'*32,'folder':'c'*32})
    def retained():
        with engine.connect() as c:
            assert c.scalar(text('select parent_id from folders where id=:id'),{'id':'c'*32})=='b'*32
            assert c.scalar(text('select folder_id from problem_placements'))=='c'*32
            assert c.scalar(text('select count(*) from problems'))==1
    migrate('upgrade','head');retained();migrate('check')
    migrate('downgrade','e72b134fa890');retained()
    migrate('upgrade','head');retained();migrate('check')
    engine.dispose()
