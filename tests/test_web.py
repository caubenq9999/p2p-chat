import base64
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from main import Application
from web_app import WebBridge, create_server


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("", 0))
            udp = sock.getsockname()[1]
        self.apps=[]
        self.servers=[]
        self.bridges=[]
        for name in ('web-a','web-b'):
            app=Application(name,0,peer_id=name,udp_port=udp)
            self.apps.append(app)
            self.addCleanup(app.stop)
            app.start()
            bridge=WebBridge(app,Path(self.temp.name)/name)
            self.bridges.append(bridge)
            server=create_server(bridge,port=0)
            self.servers.append(server)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            threading.Thread(target=server.serve_forever,daemon=True).start()
        for app in self.apps:
            app.discovery.broadcast_presence()
        self.wait(lambda: len(self.state(0)['peers'])==1 and len(self.state(1)['peers'])==1)

    def request(self,n,path,data=None,token=True):
        headers={}
        if data is not None:
            headers['Content-Type']='application/json'
            if token: headers['X-Chat-Token']=self.bridges[n].token
            data=json.dumps(data).encode()
        request=Request(f'http://127.0.0.1:{self.servers[n].server_port}{path}',data=data,headers=headers)
        with urlopen(request,timeout=10) as response:
            return response.read()

    def state(self,n):
        return json.loads(self.request(n,'/api/state'))

    def wait(self,predicate):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if predicate(): return
            time.sleep(.05)
        self.fail('Web integration timeout')

    def test_http_to_tcp_chat_and_file(self):
        for asset in ('/','/styles.css','/app.js','/assets/send.svg'):
            self.assertTrue(self.request(0,asset))
        self.request(0,'/api/send',{'peer_id':'web-b','text':'Xin chào từ web 🌍'})
        self.wait(lambda: self.state(1)['messages'].get('web-a'))
        self.assertEqual(self.state(1)['messages']['web-a'][0]['text'],'Xin chào từ web 🌍')
        content=bytes(range(256))*1024
        upload=json.loads(self.request(0,'/api/upload',{'peer_id':'web-b','filename':'test.bin','content':base64.b64encode(content).decode()}))
        tid=upload['transfer_id']
        self.wait(lambda:any(t['transfer_id']==tid for t in self.state(1)['transfers']))
        self.request(1,'/api/accept',{'transfer_id':tid})
        self.wait(lambda:any(t['transfer_id']==tid and t['status']=='completed' for t in self.state(1)['transfers']))
        self.assertEqual(self.request(1,'/api/download?id='+tid),content)
        self.wait(lambda:any(t['transfer_id']==tid and t['status']=='completed' for t in self.state(0)['transfers']))

    def test_reject_and_request_boundaries(self):
        for path in ('/chat.py','/.git/config','/assets/../chat.py'):
            with self.assertRaises(HTTPError) as result: self.request(0,path)
            self.assertEqual(result.exception.code,404)
        with self.assertRaises(HTTPError) as result:
            self.request(0,'/api/send',{'peer_id':'web-b','text':'unauthorized'},token=False)
        self.assertEqual(result.exception.code,403)
        data=json.loads(self.request(0,'/api/upload',{'peer_id':'web-b','filename':'empty.txt','content':''}))
        tid=data['transfer_id']
        self.wait(lambda:any(t['transfer_id']==tid for t in self.state(1)['transfers']))
        self.request(1,'/api/reject',{'transfer_id':tid})
        self.wait(lambda:any(t['transfer_id']==tid and t['status']=='rejected' for t in self.state(0)['transfers']))

    def test_original_filename_and_image_preview(self):
        content=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
        filename='ảnh thử.png'
        upload=json.loads(self.request(0,'/api/upload',{'peer_id':'web-b','filename':filename,'content':base64.b64encode(content).decode()}))
        tid=upload['transfer_id']
        self.wait(lambda:any(t['transfer_id']==tid for t in self.state(1)['transfers']))
        self.request(1,'/api/accept',{'transfer_id':tid})
        self.wait(lambda:any(t['transfer_id']==tid and t.get('previewable') for t in self.state(1)['transfers']))
        record=self.apps[1].files.get_transfer(tid)
        self.assertEqual(Path(record['path']).name,filename)
        from urllib.parse import quote
        for endpoint,content_type,disposition in [('download','application/octet-stream','attachment'),('preview','image/png','inline')]:
            with urlopen(f'http://127.0.0.1:{self.servers[1].server_port}/api/{endpoint}?id={tid}') as response:
                self.assertEqual(response.headers['Content-Type'],content_type)
                self.assertEqual(response.headers['Content-Disposition'],disposition+"; filename*=UTF-8''"+quote(filename,safe=''))
                self.assertEqual(response.read(),content)
