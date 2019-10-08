import logging
import os
import pytest
from six import StringIO
from user_sync import config
from user_sync.rules import RuleProcessor

@pytest.fixture
def fixture_dir():
    return os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), 'fixture'))

@pytest.fixture
def cli_args():
    def _cli_args(args_in):
        """
        :param dict args:
        :return dict:
        """

        args_out = {}
        for k in config.ConfigLoader.invocation_defaults:
            args_out[k] = None
        for k, v in args_in.items():
            args_out[k] = v
        return args_out

    return _cli_args


@pytest.fixture
def log_stream():
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger('test_logger')
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    yield stream, logger
    handler.close()




@pytest.fixture
def mock_directory_user():
    return {
        'identity_type': 'federatedID',
        'username': 'nameless@example.com',
        'domain': 'example.com',
        'firstname': 'One',
        'lastname': 'Six',
        'email': 'nameless@example.com',
        'groups': ['All Sea of Carag'],
        'country': None,
        'member_groups': [],
        'source_attributes': {
            'email': 'nameless@example.com',
            'identity_type': None,
            'username': None,
            'domain': None,
            'givenName': 'One',
            'sn': 'Six',
            'c': 'US'}}

@pytest.fixture()
def mock_umapi_user():
    return  {
        'email': 'bsisko@example.com',
        'status': 'active',
        'groups': ['Group A', '_admin_Group A', 'Group A_1924484-provisioning'],
        'username': 'bsisko@example.com',
        'domain': 'example.com',
        'firstname': 'Benjamin',
        'lastname': 'Sisko',
        'country': 'CA',
        'type': 'federatedID'
    }