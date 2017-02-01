from __future__ import print_function
import xmlrpclib
import time
import sys
from smartcard.scard import *
import smartcard.util
import pickle
import json



class OdooRemote():

    #url = 'https://odoo.fablab-karlsruhe.de'
    url = None
    db = None
    username = 'admin'
    password = 'admin'
    machine_name = 'LaserSaur' #TODO get machine name from Odoo (new machine field)
    user_level = None
    unlock_time = 5 #how long is the machine unlocked?
    dummy_mode = False
    sell_mode = False
    last_user = ''

    _common = None
    _uid = None
    _models = None
    _mode = None
    _id_cards = None
    _users = None
    _last_acces = 0
    products_product = None
    _product_category = None
    _material_tag_id = 0
    _laser_tag_id = 0
    materials = None
    services = None


    #def main(self):
    #    self.init()
    #    while(True):
    #        self.get_access()
    #        if self._mode == 'error':
    #            return False
    #        #if self.mode == 'backup':
    #            #TODO: Try to go live again
    #        time.sleep(1)

    def __init__(self, username, password, url, db):
        self.username = username
        self.password = password
        self.url = url
        self.db = db
        self.init()

    def init(self):
        print("Initializing Odoo remote connection")
        if self.dummy_mode:
            self.last_user = 'Max Mustermann'
        try:
            self._common = xmlrpclib.ServerProxy('{}/xmlrpc/2/common'.format(self.url))
            self._uid = self._common.authenticate(self.db, self.username, self.password, {})
            self._models = xmlrpclib.ServerProxy('{}/xmlrpc/2/object'.format(self.url))
            #TODO: Save ID-Cards, Users and Machines locally (in case of odoo/internet failure)
            self._machine = self._models.execute_kw(self.db, self._uid, self.password,
                                    'lab.machine', 'search_read',
                                    [[['name', '=', self.machine_name]]],
                                    {})[0]
            #print("Machine:")
            #print(self._machine)

            self._material_tag_id = self._machine['machine_tag_1'][0]
            #print("Material Tag ID:")
            #print(self._material_tag_id)

            self._laser_tag_id = self._machine['machine_tag_2'][0]
            #print("Laser Service Tag ID:")
            #print(self._laser_tag_id)


            self._id_cards = self._models.execute_kw(self.db, self._uid, self.password,
                                    'lab.id_cards', 'search_read',
                                    [],
                                    {'fields':['card_id', 'status', 'assigned_client']})
            #print("ID Cards: ")
            #print(self._id_cards)

            self._users = self._models.execute_kw(self.db, self._uid, self.password,
                                    'res.partner', 'search_read',
                                    [],
                                    {'fields':['id', 'name']})
            #print("Users:")
            #print(self._users)

            self.materials = self._models.execute_kw(self.db, self._uid, self.password,
                                    'product.template', 'search_read',
                                                     [[['tag_ids', '=', self._material_tag_id]]],
                                                     {})
            self.services = self._models.execute_kw(self.db, self._uid, self.password,
                                    'product.template', 'search_read',
                                                    [[['tag_ids', '=', self._laser_tag_id]]],
                                                    {})
            #print("Materials:")
            #for mat in self.materials:
            #    print(str(mat['id']) + "   " + mat['name'])
            #print("Services:")
            #for serv in self.services:
            #    print(str(serv['id']) + "   " + serv['name'])

            with open('machine.json', 'w') as file:
                json.dump(self._machine, file, indent=4, separators=(',', ': '))
            with open('id_cards.json', 'w') as file:
                json.dump(self._id_cards, file, indent=4, separators=(',', ': '))
            with open('users.json', 'w') as file:
                json.dump(self._users, file, indent=4, separators=(',', ': '))
            with open('materials.json', 'w') as file:
                json.dump(self.materials, file, indent=4, separators=(',', ': '))
            with open('services.json', 'w') as file:
                json.dump(self.services, file, indent=4, separators=(',', ': '))

            print("Odoo Database loaded and saved locally")
            self._mode = 'odoo'


        except IOError:
            print("Couldn't open Database, trying to load backup...")
            try:
                with open('machine.json') as file:
                    self._machine = json.load(file)
                with open('id_cards.json') as file:
                    self._id_cards = json.load(file)
                with open('users.json') as file:
                    self._users = json.load(file)
                with open('materials.json') as file:
                    self.materials = json.load(file)
                with open('services.json') as file:
                    self.services = json.load(file)
                self._mode='backup'
                print("Backup loaded!")
            except IOError:
                #TODO: Maybe just throw an error message?
                print("Couldn't find or load local backup Database.")
                print("Shutting down LaserSaurApp, try starting without Odoo support.")
                exit(1)
                self._mode='error'

        if not self._machine:
            print("Odoo error: No matching machine found")
            return False

    def check_access(self, card_number):
        if self.dummy_mode:
            return "Max Mustermann"
        if card_number == None:
            return False
        card_number = card_number.upper()
        #print("card: " + card_number + " ", end="")

        # if self._mode == 'odoo':
        #     try:
        #         self._machine = self._models.execute_kw(self.db, self._uid, self.password,
        #                                 'lab.machine', 'search_read',
        #                                 [[['name', '=', self.machine_name]]],
        #                                 {})[0]
        #         card = self._models.execute_kw(self.db, self._uid, self.password,
        #                                 'lab.id_cards', 'search_read',
        #                                 [[['card_id', '=', card_number]]],
        #                                 {})
        #     except IOError:
        #         print("CONNECTIN_FAILED")
        #         self._mode = 'backup'
        # if self._mode == 'backup':
        card = filter(lambda x: x['card_id'] == card_number, self._id_cards)
        if len(card) != 1:
            #print("CARD_NOT_FOUND")
            return False
        #print(card[0])
        if card[0]['status'] != 'a':
            print("Odoo warning: Card is not active!")
            return False
        if card[0]['assigned_client'] == 0:
            print("Odoo warning: Card is not assigned")
            return False
        # if self._mode == 'odoo':
        #     try:
        #         client = self._models.execute_kw(self.db, self._uid, self.password,
        #                                 'res.partner', 'read',
        #                                 [[card[0]['assigned_client'][0]]],
        #                                 {'fields': ['name']})
        #     except IOError:
        #         print("CONNECTIN_FAILED")
        #         self._mode = 'backup'
        # if self._mode == 'backup':
        client = filter(lambda user: user['id'] == card[0]['assigned_client'][0], self._users)

        if(len(client) != 1):
            print("Odoo Error: Fucked up everything, contact Philip Caroli")
            self._mode = 'error'
            return False
        client = client[0]
        if client['id'] in self._machine['owner_ids']:
            #print("CLIENT_IS_OWNER: " + client['name'])
            self.user_level = 'owner'
            self.last_user = client['name']
            return client['name'] #Owners get full acces no matter the circumstances
        elif self._machine['status'] == 'r':
            if client['id'] in self._machine['user_ids'] and self._machine['rules'] == 'r':
                #print("CLIENT_IS_USER: " + client['name'])
                self.user_level = 'user'
                self.last_user = client['name']
                return client['name']
            elif self._machine['rules'] == 'f':
                #print("free access")
                self.user_level = 'free'
                self.last_user = client['name']
                return client['name']
        #print("no acces for " + client['name'])
        return False

    def get_access(self):
        if self.get_access_rfid():
            self._last_acces = time.time()
            #print("ACCESS_UNLOCKED")
            return True
        else:
            if (time.time() - self._last_acces) < self.unlock_time:
                #print("STILL_UNLOCKED")
                return True
        return False

    def get_product(self, id):
        ret_product = filter(lambda product: product['id'] == int(id), self.materials)
        if len(ret_product) != 1:
            #print("product " + id + " not found!")
            return None
        return ret_product[0]

    def get_service(self, id):
        ret_service = filter(lambda service: service['id'] == int(id), self.services)
        if len(ret_service) != 1:
            #print("service " + id + " not found!")
            return None
        return ret_service[0]



