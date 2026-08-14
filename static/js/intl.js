(function (global) {
    'use strict';

    function locale(language) {
        // The site's English date convention is day/month/year. All other
        // supported language codes can be passed directly to Intl.
        return language === 'en' ? 'en-GB' : (language || 'en-GB');
    }

    function format(value, language, options) {
        var date = value instanceof Date ? value : new Date(value);
        if (Number.isNaN(date.getTime())) {
            return '';
        }
        return new Intl.DateTimeFormat(locale(language), options).format(date);
    }

    function withTimeZone(options, timeZone) {
        if (timeZone) {
            options.timeZone = timeZone;
        }
        return options;
    }

    function date(value, language, timeZone) {
        var options = language === 'en'
            ? { day: '2-digit', month: '2-digit', year: 'numeric' }
            : { dateStyle: 'medium' };
        return format(value, language, withTimeZone(options, timeZone));
    }

    function dateTime(value, language, timeZone) {
        var options = language === 'en'
            ? {
                day: '2-digit', month: '2-digit', year: 'numeric',
                hour: '2-digit', minute: '2-digit', hour12: false,
            }
            : { dateStyle: 'medium', timeStyle: 'short', hour12: false };
        return format(value, language, withTimeZone(options, timeZone));
    }

    function time(value, language, timeZone, includeSeconds) {
        var options = {
            hour: '2-digit', minute: '2-digit', hour12: false,
        };
        if (includeSeconds) {
            options.second = '2-digit';
        }
        return format(value, language, withTimeZone(options, timeZone));
    }

    global.summonIntl = Object.freeze({
        locale: locale,
        date: date,
        dateTime: dateTime,
        time: time,
    });
}(window));
