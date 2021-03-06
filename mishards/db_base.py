import logging
from sqlalchemy import create_engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.orm.session import Session as SessionBase

logger = logging.getLogger(__name__)


class LocalSession(SessionBase):
    def __init__(self, db, autocommit=False, autoflush=True, **options):
        self.db = db
        bind = options.pop('bind', None) or db.engine
        SessionBase.__init__(self, autocommit=autocommit, autoflush=autoflush, bind=bind, **options)


class DB:
    Model = declarative_base()

    def __init__(self, uri=None, echo=False):
        self.echo = echo
        uri and self.init_db(uri, echo)
        self.session_factory = scoped_session(sessionmaker(class_=LocalSession, db=self))

    def init_db(self, uri, echo=False):
        url = make_url(uri)
        if url.get_backend_name() == 'sqlite':
            self.engine = create_engine(url)
        else:
            self.engine = create_engine(uri, pool_size=100, pool_recycle=5, pool_timeout=30,
                                        pool_pre_ping=True,
                                        echo=echo,
                                        max_overflow=0)
        self.uri = uri
        self.url = url

    def __str__(self):
        return '<DB: backend={};database={}>'.format(self.url.get_backend_name(), self.url.database)

    @property
    def Session(self):
        return self.session_factory()

    def remove_session(self):
        self.session_factory.remove()

    def drop_all(self):
        self.Model.metadata.drop_all(self.engine)

    def create_all(self):
        self.Model.metadata.create_all(self.engine)
