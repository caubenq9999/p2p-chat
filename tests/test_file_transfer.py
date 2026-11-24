import base64
import hashlib
from pathlib import Path
import queue
import tempfile
import unittest

from file_transfer import FileTransferManager, TransferRecord, TransferDirection, TransferStatus


class Transport:
    def __init__(self):
        self.packets = []

    def send_packet(self, peer_id, packet):
        self.packets.append(packet)
        return True


class FileStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.transport = Transport()
        self.manager = FileTransferManager(self.transport, queue.Queue(), local_peer_id='a')
        self.addCleanup(self.manager.shutdown)

    def receive_offer(self):
        self.manager.handle_packet({'type':'FILE_OFFER','sender_id':'b','payload':{
            'transfer_id':'t','filename':'test.txt','size':3,'sha256':hashlib.sha256(b'abc').hexdigest()}})

    def test_early_failure_stops_sender_and_foreign_peer_cannot_abort(self):
        record=TransferRecord('t','b','test.txt',3,'',TransferDirection.OUTGOING,TransferStatus.SENDING)
        self.manager._transfers['t']=record
        packet={'type':'FILE_RESULT','sender_id':'other','payload':{'transfer_id':'t','ok':False,'reason':'disk full'}}
        self.manager.handle_packet(packet)
        self.assertEqual(record.status,TransferStatus.SENDING)
        packet['sender_id']='b'
        self.manager.handle_packet(packet)
        self.assertEqual(record.status,TransferStatus.FAILED)
        self.assertEqual(record.reason,'disk full')

    def test_cancel_received_file_deletes_partial_and_ignores_late_end(self):
        self.receive_offer()
        destination=Path(self.temp.name)/'received.txt'
        self.assertTrue(self.manager.accept_file('t',str(destination)))
        self.manager.handle_packet({'type':'FILE_CHUNK','sender_id':'b','payload':{
            'transfer_id':'t','seq':0,'data':base64.b64encode(b'a').decode()}})
        self.assertTrue(self.manager.cancel_transfer('t'))
        self.manager.handle_packet({'type':'FILE_END','sender_id':'b','payload':{'transfer_id':'t'}})
        self.assertFalse(destination.exists())
        self.assertEqual(list(Path(self.temp.name).iterdir()),[])
        self.assertFalse(self.transport.packets[-1]['payload']['ok'])
        self.assertFalse(self.manager.cancel_transfer('t'))

    def test_failed_destination_notifies_sender(self):
        self.receive_offer()
        parent=Path(self.temp.name)/'not-a-directory'
        parent.write_text('occupied')
        self.assertFalse(self.manager.accept_file('t',str(parent/'file.txt')))
        self.assertEqual(self.transport.packets[-1]['type'],'FILE_RESULT')
        self.assertFalse(self.transport.packets[-1]['payload']['ok'])

    def test_remote_cancel_cleans_incoming_partial(self):
        self.receive_offer()
        self.assertTrue(self.manager.accept_file('t',str(Path(self.temp.name)/'received.txt')))
        self.manager.handle_packet({'type':'FILE_RESULT','sender_id':'b','payload':{'transfer_id':'t','ok':False,'reason':'cancelled'}})
        self.assertEqual(self.manager.get_transfer('t')['status'],'failed')
        self.assertEqual(list(Path(self.temp.name).iterdir()),[])
