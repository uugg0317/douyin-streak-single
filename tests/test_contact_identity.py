"""Identity regressions use fresh temporary ledger data, never an account state."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]

class ContactIdentityTests(unittest.TestCase):
    def isolated(self, body):
        with tempfile.TemporaryDirectory(prefix="spark-identity-") as temporary:
            env = dict(os.environ)
            env.update(DATA_DIR=str(Path(temporary)/"data"), ENV_FILE_PATH=str(Path(temporary)/"missing.env"),
                       STATE_FILE_PATH="", SPARKKEEPER_BACKGROUND_TASKS="0", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
            prelude = "from core import ledger as L\nimport json\n"
            completed = subprocess.run([sys.executable, "-B", "-c", prelude + textwrap.dedent(body)],
                                       cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_legacy_migration_persists_ids_and_preserves_history(self):
        self.isolated("""
        L.DATA_DIR.mkdir(parents=True)
        L.LEDGER_PATH.write_text(json.dumps([{'display_name':'A','selected':True,'has_conversation':True,'last_sent_at':'history'},
                                             {'display_name':'A','selected':False,'has_conversation':True,'last_sent_at':None}]))
        first=L.load_ledger();second=L.load_ledger()
        assert len(first)==2 and first[0]['selected'] and first[0]['last_sent_at']=='history'
        assert first[0]['entry_id']!=first[1]['entry_id']
        assert [x['entry_id'] for x in first]==[x['entry_id'] for x in second]
        assert all(x['identity_ambiguous'] for x in first)
        assert all(x.get('entry_id') for x in json.loads(L.LEDGER_PATH.read_text()))
        """)

    def test_same_names_are_retained_and_id_operations_are_isolated(self):
        self.isolated("""
        L.merge_consumer_contacts([{'name':'Same','avatar':'A','streak':4},{'name':'Same','avatar':'B','streak':9}])
        records=L.load_ledger();assert len(records)==2 and {r['avatar'] for r in records}=={'A','B'}
        ids=[r['entry_id'] for r in records]
        L.set_selected([{'entry_id':ids[0],'selected':True}]);assert [x['selected'] for x in L.load_ledger()]==[True,False]
        L.update_send_result('Same',False,entry_id=ids[1],status='unconfirmed')
        assert [x.get('last_status') for x in L.load_ledger()]==['pending','unconfirmed']
        try:L.remove_entry('Same')
        except L.IdentityConflict:pass
        else:raise AssertionError('ambiguous legacy delete was accepted')
        assert len(L.load_ledger())==2
        assert L.remove_entry(entry_id=ids[0]) and L.load_ledger()[0]['entry_id']==ids[1]
        """)

    def test_creator_does_not_join_by_nickname_or_overwrite_platform_id(self):
        self.isolated("""
        L.merge_consumer_contacts([{'name':'Same','avatar':'consumer'}])
        consumer=L.load_ledger()[0]['entry_id']
        L.merge_creator_map({'short-A':{'nickname':'Same','user_id':'user-A'},'short-B':{'nickname':'Same','user_id':'user-B'}})
        records=L.load_ledger();assert len(records)==3
        assert {r.get('user_id') for r in records}=={'','user-A','user-B'}
        assert next(r for r in records if r['entry_id']==consumer)['user_id']==''
        assert next(r for r in records if r['entry_id']==consumer)['identity_ambiguous'] is False
        assert all(r['identity_ambiguous'] for r in records if r['entry_id']!=consumer)
        L.merge_creator_map({'renamed-short-A':{'nickname':'Renamed','user_id':'user-A'}})
        records2=L.load_ledger();assert len(records2)==3
        a=next(r for r in records2 if r.get('user_id')=='user-A');assert a['short_id']=='renamed-short-A'
        b=next(r for r in records2 if r.get('user_id')=='user-B');assert b['short_id']=='short-B'
        """)

    def test_platform_ids_only_join_when_explicitly_verified(self):
        self.isolated("""
        L.merge_creator_map({'short-A':{'nickname':'A','user_id':'user-A'}})
        first=L.load_ledger()[0]['entry_id']
        L.merge_consumer_contacts([{'name':'Renamed','avatar':'A','user_id':'user-A','identity_verified':True}])
        assert len(L.load_ledger())==1 and L.load_ledger()[0]['entry_id']==first
        before=L.load_ledger()[0]
        try:L._upsert([before],{'entry_id':first,'display_name':'Renamed','user_id':'user-B'})
        except L.IdentityConflict:pass
        else:raise AssertionError('conflicting platform ID overwrote record')
        assert before['user_id']=='user-A'
        """)

    def test_homonymous_repeated_collection_is_stable(self):
        self.isolated("""
        contacts=[{'name':'Same','avatar':'A'},{'name':'Same','avatar':'B'}]
        L.merge_consumer_contacts(contacts);before=[e['entry_id'] for e in L.load_ledger()]
        contacts[0]['streak']=5;contacts[1]['streak']=9
        contacts[0]['row_key']='Same|A|5|1';contacts[1]['row_key']='Same|B|9|1'
        L.merge_consumer_contacts(contacts);assert [e['entry_id'] for e in L.load_ledger()]==before
        """)

    def test_legacy_name_route_conflicts_but_id_route_changes_one_record(self):
        self.isolated("""
        from routers import contacts_ledger as R
        from models import LedgerBody, DeleteFriendBody
        from fastapi import HTTPException
        R._check_auth=lambda token:None
        L.merge_consumer_contacts([{'name':'Same','avatar':'A'},{'name':'Same','avatar':'B'}])
        records=L.load_ledger()
        try:R.api_ledger_save(LedgerBody(entries=[{'display_name':'Same','selected':True}]),token='fixture')
        except HTTPException as error:assert error.status_code==409
        else:raise AssertionError('legacy name route accepted ambiguity')
        assert not any(r['selected'] for r in L.load_ledger())
        R.api_ledger_save(LedgerBody(entries=[{'entry_id':records[0]['entry_id'],'selected':True}]),token='fixture')
        assert [r['selected'] for r in L.load_ledger()]==[True,False]
        R.api_ledger_delete(DeleteFriendBody(entry_id=records[1]['entry_id']),token='fixture')
        assert L.load_ledger()[0]['entry_id']==records[0]['entry_id']
        """)

if __name__ == '__main__':unittest.main()
