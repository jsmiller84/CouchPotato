from app.config.db import Movie, Session as Db
from app.lib.cron.cronBase import cronBase
from app.lib.provider.rss import rss
from app.lib.qualities import Qualities
from sqlalchemy.sql.expression import or_
from urllib2 import URLError
import cherrypy
import datetime
import logging
import os
import time
import urllib
import urllib2

log = logging.getLogger(__name__)

class YarrCron(cronBase, rss):

    ''' Cronjob for searching for nzb/torrents '''

    lastChecked = 0
    provider = None
    sabNzbd = None
    intervalSec = 10
    checkTheseMovies = []
    stop = False

    def run(self):
        log.info('YarrCron thread is running.')

        self.setInterval(self.config.get('Intervals', 'search'))
        self.forceCheck()

        if not self.debug:
            time.sleep(10)

        wait = 0.1 if self.debug else 1
        while True and not self.abort:

            #check single movie
            for movieId in self.checkTheseMovies:
                movie = Db.query(Movie).filter_by(id = movieId).one()
                self._search(movie, True)
                self.checkTheseMovies.pop(0)

            #check all movies
            now = time.time()
            if (self.lastChecked + self.intervalSec) < now:# and not self.debug:
                self.lastChecked = now
                self.searchAll()

            time.sleep(wait)

        log.info('YarrCron has shutdown.')

    def setInterval(self, interval):
        self.intervalSec = int(interval) * 60 * 60

    def forceCheck(self, movie = None):
        if movie == None:
            self.lastChecked = time.time() - self.intervalSec + 10
        else:
            self.checkTheseMovies.append(movie)

    def stopCheck(self):
        log.info('Forcing search to stop.')
        self.stop = True

    def searchAll(self):
        log.info('Searching for new download for all movies.')
        self.doCheck()

        #get all wanted movies
        movies = Db.query(Movie).filter(or_(Movie.status == 'want', Movie.status == 'waiting')).all()
        for movie in movies:
            if not self.abort and not self.stop:
                self._search(movie)

        self.doCheck(False)
        log.info('Finished search.')


    def _search(self, movie, force = False):

        # Stop caching ffs!
        Db.expire_all()

        # Check release date and search for appropriate qualities
        preReleaseSearch = False
        dvdReleaseSearch = False
        now = int(time.time())

        # Search all if ETA is unknow, but try update ETA for next time.
        checkETA = False
        if not movie.eta or force:
            checkETA = True
            preReleaseSearch = True
            dvdReleaseSearch = True
        else:
            # Prerelease 1 week before theaters
            if movie.eta.theater <= now + 604800:
                preReleaseSearch = True

            # dvdRelease 2 weeks before dvd release
            if movie.eta.dvd <= now + 1209600:
                preReleaseSearch = True
                dvdReleaseSearch = True

            # Dvd date is unknown but movie is in theater already
            if movie.eta.dvd == 0 and movie.eta.theater > now:
                checkETA = True
                dvdReleaseSearch = False

            # Force ETA check once a week
            if movie.eta.dvd == 0 or movie.eta.theater == 0:
                checkETA = True

            # Minimal week interval for ETA check
            if checkETA and movie.eta.lastCheck < now - 604800:
                cherrypy.config.get('searchers').get('etaQueue').put({'id':movie.id})

        for queue in movie.queue:

            # Movie already found, don't search further 
            if queue.completed:
                log.debug('%s already completed for "%s". Not searching for any qualities below.' % (queue.qualityType, movie.name))
                return True

            # only search for active and not completed, minimal 1 min since last search
            if queue.active and not queue.completed and not self.abort and not self.stop:

                #skip if no search is set
                if not ((preReleaseSearch and queue.qualityType in Qualities.preReleases) or (dvdReleaseSearch and not queue.qualityType in Qualities.preReleases)):
                    continue

                highest = self.provider.find(movie, queue)

                #send highest to SABnzbd & mark as snatched
                if highest:

                    #update what I found
                    queue.name = highest.name
                    queue.link = highest.detailUrl

                    waitFor = queue.waitFor * (60 * 60 * 24)

                    if queue.markComplete or (not queue.markComplete and highest.date + waitFor < time.time()):
                        time.sleep(10) # Give these APIs air!
                        if self.config.get('NZB', 'sendTo') == 'Sabnzbd' and highest.type == 'nzb':
                            success = self.sabNzbd.send(highest)
                        else:
                            success = self.blackHole(highest)
                    else:
                        success = False
                        log.info('Found %s but waiting for %d hours.' % (highest.name, ((highest.date + waitFor) - time.time()) / (60 * 60)))

                    # Set status
                    if success:
                        movie.status = u'snatched' if queue.markComplete else u'waiting'
                        movie.dateChanged = datetime.datetime.now()
                        queue.lastCheck = now
                        queue.completed = True
                        Db.flush()

                    return True

                if not self.debug:
                    time.sleep(5)

                queue.lastCheck = now
                Db.flush()

        return False

    def blackHole(self, item):
        blackhole = self.config.get('NZB', 'blackhole') if item.type == 'nzb' else self.config.get('Torrents', 'blackhole')
        if not blackhole or not os.path.isdir(blackhole):
            log.error('No directory set for blackhole %s download.' % item.type)
        else:
            fullPath = os.path.join(blackhole, self.toSaveString(item.name) + '.' + item.type)

            if not os.path.isfile(fullPath):
                log.info('Downloading %s to %s.' % (item.type, fullPath))
                file = urllib.urlopen(item.url).read()
                with open(fullPath, 'wb') as f:
                    f.write(file)

                return True
            else:
                log.error('File %s already exists.' % fullPath)

        return False

    def doCheck(self, bool = True):
        self.running = bool
        self.lastChecked = time.time()
        self.stop = False

    def isChecking(self):
        return self.running

    def lastCheck(self):
        return self.lastChecked

    def nextCheck(self):

        seconds = int((self.lastChecked + self.intervalSec) - time.time())
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        w, d = divmod(d, 7)

        ds = ''
        if w > 0:
            ds += '%d week%s' % (w, 's' if w != 1 else '')
        if d > 0:
            ds += '%d day%s' % (d, 's' if d != 1 else '')
        if h > 0:
            ds += '%d hour%s' % (h, 's' if h != 1 else '')
        if m > 0:
            ds += ' %d minute%s' % (m, 's' if m != 1 else '')
        if s > 0 and not h > 0 and not m > 15:
            ds += ' %d second%s' % (s, 's' if s != 1 else '')

        return {
            'seconds': seconds,
            'string': ds
        }

def startYarrCron(config, debug, provider):
    cron = YarrCron()
    cron.config = config
    cron.debug = debug
    cron.provider = provider
    cron.start()

    return cron
