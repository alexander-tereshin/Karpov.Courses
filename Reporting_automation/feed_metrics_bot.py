import json
import os
import io
from datetime import date, datetime, timedelta

import pandahouse as ph
import matplotlib.pyplot as plt
import telegram
from airflow.decorators import dag, task

load_dotenv()


default_args = {
    'owner': 'a-tereshin',
    'depends_on_past': False,
    'retries': 1,
    'retry_delay': timedelta(seconds=300),
    'start_date': datetime(2022, 2, 8)
}
schedule_interval = '59 10 * * *'  # cron-expression (daily at 10:59 am)


@dag(default_args=default_args, schedule_interval=schedule_interval, catchup=False)
def atereshin_bot_feed_dag():
    @task()
    def load_df():
        query = """
            select 
                toDate(time) as date,
                count(distinct user_id) dau,
                countIf(action='view') views,
                countIf(action='like') likes,
                round(countIf(action='like') * 100 / countIf(action='view'), 1) ctr
            from simulator_20230120.feed_actions
            where toDate(time) between (today() - 7) and yesterday()
            group by date
            """
        connection = json.loads(os.environ.get('DB_CONNECTION'))
        dataframe = ph.read_clickhouse(query, connection=connection)
        return dataframe

    @task()
    def get_metrics(df):
        dau_yesterday = df[df.date == df.date.max()].dau.item()
        views_yesterday = df[df.date == df.date.max()].views.item()
        likes_yesterday = df[df.date == df.date.max()].likes.item()
        ctr_yesterday = df[df.date == df.date.max()].ctr.item()
        metrics_dict = {'dau': dau_yesterday, 'views': views_yesterday, 'likes': likes_yesterday, 'ctr': ctr_yesterday}
        return metrics_dict

    @task()
    def get_plot(df):
        plt_date = df['date'].dt.strftime('%d %b')
        plt.figure(figsize=(50, 30))
        plt.rcParams.update({'font.size': 21})

        plt.suptitle(f'News Feed Metrics\n{(date.today() - timedelta(days=1)).strftime("%d/%m/%Y")}', y=0.94)
        plt.subplots_adjust(wspace=0.2, hspace=0.25)

        plt.subplot(221)
        plt.plot(plt_date, df.dau, color='darkcyan', marker='o', markerfacecolor='w')
        plt.title('DAU', pad=20)
        plt.ylabel('Users', labelpad=20)
        plt.grid(True)

        plt.subplot(222)
        plt.plot(plt_date, df.ctr, color='darkcyan', marker='o', markerfacecolor='w')
        plt.title('CTR', pad=20)
        plt.ylabel('CTR, %', labelpad=20)
        plt.grid(True)

        plt.subplot(223)
        plt.plot(plt_date, df.likes, color='darkcyan', marker='o', markerfacecolor='w')
        plt.title('Likes', pad=20)
        plt.ylabel('Likes', labelpad=20)
        plt.grid(True)

        plt.subplot(224)
        plt.plot(plt_date, df.views, color='darkcyan', marker='o', markerfacecolor='w')
        plt.title('Views', pad=20)
        plt.ylabel('Views', labelpad=20)
        plt.grid(True)

        plot = io.BytesIO()
        plt.savefig(plot)
        plt.close()
        plot.seek(0)

        return plot


    @task()
    def load_report(metrics_dict, plot):
        chat_id = json.loads(os.environ.get('TOKEN'))
        token = json.loads(os.environ.get('TOKEN'))
        bot = telegram.Bot(token=token)

        message = "Ежедневный отчет с информацией о значениях ключевых метрик за вчерашний день.\n\n" \
                  f"На *{(date.today() - timedelta(days=1)).strftime('%d/%m/%Y')}*:\n" \
                  f"*DAU* : {metrics_dict['dau']}\n" \
                  f"*Просмотры* : {metrics_dict['views']}\n" \
                  f"*Лайки* : {metrics_dict['likes']}\n" \
                  f"*CTR* : {metrics_dict['ctr']}%\n\n" \

        bot.sendMessage(chat_id=chat_id, text=message, parse_mode=telegram.ParseMode.MARKDOWN)

        bot.sendPhoto(chat_id=chat_id, photo=plot, caption="График с значениями метрик за предыдущие 7 дней.")



    df_feed = load_df()
    metrics = get_metrics(df_feed)
    plot = get_plot(df_feed)
    load_report(metrics, plot)


atereshin_bot_feed_dag = atereshin_bot_feed_dag()

