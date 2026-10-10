import copy
import unittest

from decision import validate_market_forecast, validate_visual_ai
from daily_ai import expand_output, MODEL_SCHEMA


def forecast_fixture(target='2026-09-30'):
    return {'basis_date':target,'horizon_trading_days':5,
        'next_day':'预计分化，若指数站上均线再重新评估。',
        'near_term':'未来3—5交易日观察上涨家数是否持续改善。',
        'scenarios':{key:{'outlook':'条件性走势预测','trigger':'上涨家数增加',
            'invalidation':'上涨家数再度减少'} for key in ('基准','偏强','偏弱')},
        'confidence':'低','confidence_reason':'资金持续性资料不足',
        'source_ids':['market']}


class MarketForecastTest(unittest.TestCase):
    def test_forecast_requires_date_horizon_sources_and_three_complete_scenarios(self):
        good=forecast_fixture();before=copy.deepcopy(good)
        validate_market_forecast(good,'2026-09-30',{'market':{}})
        self.assertEqual(good,before)
        for change in ('date','horizon','boolean_horizon','source','scenario','condition','probability','scenario_probability','confidence'):
            with self.subTest(change=change):
                bad=copy.deepcopy(good)
                if change=='date':bad['basis_date']='2026-10-07'
                if change=='horizon':bad['horizon_trading_days']=0
                if change=='boolean_horizon':bad['horizon_trading_days']=True
                if change=='source':bad['source_ids']=['unknown']
                if change=='scenario':del bad['scenarios']['偏弱']
                if change=='condition':bad['scenarios']['偏强']['invalidation']=''
                if change=='probability':bad['probability']=.9
                if change=='scenario_probability':bad['scenarios']['偏强']['probability']=.9
                if change=='confidence':bad['confidence']='90%'
                with self.assertRaises(ValueError):
                    validate_market_forecast(bad,'2026-09-30',{'market':{}})

    def test_new_transport_cannot_silently_omit_prediction(self):
        with self.assertRaisesRegex(ValueError,'必须包含预测'):
            expand_output({'schema_version':MODEL_SCHEMA,'market':{}},{},{},{})

    def test_detailed_market_judgment_retains_other_length_limits(self):
        data={'schema_version':'decision-ui-v1','market':{'stance':'等待确认',
            'judgment':'研判'*200,'headline':'等待','confirmation':'原条件','risk':'资料不足'},
            'sectors':[],'stocks':[]}
        validate_visual_ai(data)
        data['market']['judgment']='研判'*301
        with self.assertRaises(ValueError):validate_visual_ai(data)
